//! Filtering HTTP proxy for browser tabs (browser design PR 5, B4/B6).
//!
//! Tabs are configured with `WebviewBuilder::proxy_url(http://127.0.0.1:<port>)`
//! so **every** request they make (documents, `fetch`, `<img>`, WebSocket…)
//! comes here. For each request the proxy resolves the host itself, keeps only
//! the addresses the policy allows (by default [`crate::is_public_ip`]) and
//! connects to one of those exact addresses. Checking after DNS — and never
//! resolving twice — is what defeats DNS rebinding to 127.0.0.1 or the LAN.
//!
//! Supported: `CONNECT host:port` (HTTPS, WSS) and absolute-form plain HTTP
//! (`GET http://host/path`, forwarded as origin-form with `Connection: close`).
//! Anything else gets `400`. Blocked destinations get `403`.
//!
//! std only (thread per connection, bounded); tested on Linux CI.

use std::collections::VecDeque;
use std::io::{self, Read, Write};
use std::net::{IpAddr, Shutdown, SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::{check_host, Verdict};

const MAX_HEAD: usize = 16 * 1024;
const MAX_CONNS: usize = 256;
const IO_TIMEOUT: Duration = Duration::from_secs(30);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// Decides whether a resolved address may be contacted.
pub type AllowIp = fn(IpAddr) -> bool;

const MAX_RECENT_BLOCKS: usize = 32;
static RECENT_BLOCKS: Mutex<VecDeque<(Instant, String)>> = Mutex::new(VecDeque::new());

/// A tab whose request the proxy refuses only sees a failed load: WebKit
/// shows nothing and keeps the previous page. The proxy notes the host so
/// the browser UI can say why (a public name that points to 127.0.0.1 or the
/// LAN looks fine to the URL bar check and is only caught here).
fn note_block(host: &str) {
    let mut q = RECENT_BLOCKS.lock().unwrap_or_else(|e| e.into_inner());
    if q.len() >= MAX_RECENT_BLOCKS {
        q.pop_front();
    }
    q.push_back((Instant::now(), host.chars().take(253).collect()));
}

/// Hosts blocked in the last `within`, oldest first (duplicates removed).
pub fn recent_blocks(within: Duration) -> Vec<String> {
    let q = RECENT_BLOCKS.lock().unwrap_or_else(|e| e.into_inner());
    let mut out: Vec<String> = Vec::new();
    for (at, host) in q.iter() {
        if at.elapsed() <= within && !out.contains(host) {
            out.push(host.clone());
        }
    }
    out
}

pub struct ProxyHandle {
    pub addr: SocketAddr,
    stop: Arc<AtomicBool>,
}

impl ProxyHandle {
    /// `http://127.0.0.1:<port>` — what goes into `proxy_url`.
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(200));
    }
}

/// Start the proxy on 127.0.0.1 with a random port.
pub fn spawn(allow: AllowIp) -> io::Result<ProxyHandle> {
    spawn_with(allow, true)
}

/// `check_names = false` only for tests that use "localhost" as an upstream.
#[doc(hidden)]
pub fn spawn_with(allow: AllowIp, check_names: bool) -> io::Result<ProxyHandle> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    let addr = listener.local_addr()?;
    let stop = Arc::new(AtomicBool::new(false));
    let stop2 = stop.clone();
    let active = Arc::new(AtomicUsize::new(0));
    thread::Builder::new()
        .name("cl-browser-proxy".into())
        .spawn(move || {
            for conn in listener.incoming() {
                if stop2.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(client) = conn else { continue };
                if active.load(Ordering::SeqCst) >= MAX_CONNS {
                    let _ = respond(&client, 503, "proxy busy");
                    continue;
                }
                active.fetch_add(1, Ordering::SeqCst);
                let active2 = active.clone();
                let _ = thread::Builder::new()
                    .name("cl-browser-proxy-conn".into())
                    .spawn(move || {
                        let _ = handle(client, allow, check_names);
                        active2.fetch_sub(1, Ordering::SeqCst);
                    });
            }
        })?;
    Ok(ProxyHandle { addr, stop })
}

fn respond(mut s: &TcpStream, code: u16, msg: &str) -> io::Result<()> {
    let reason = match code {
        400 => "Bad Request",
        403 => "Forbidden",
        502 => "Bad Gateway",
        _ => "Service Unavailable",
    };
    let body = format!("Blocked by ConductLoop browser proxy: {msg}\n");
    write!(
        s,
        "HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

/// Request head (up to the blank line) and any bytes read past it.
fn read_head(s: &mut TcpStream) -> io::Result<(Vec<u8>, Vec<u8>)> {
    let mut buf = Vec::with_capacity(2048);
    let mut chunk = [0u8; 2048];
    loop {
        let n = s.read(&mut chunk)?;
        if n == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        buf.extend_from_slice(&chunk[..n]);
        if let Some(i) = find(&buf, b"\r\n\r\n") {
            let rest = buf.split_off(i + 4);
            return Ok((buf, rest));
        }
        if buf.len() > MAX_HEAD {
            return Err(io::ErrorKind::InvalidData.into());
        }
    }
}

fn find(h: &[u8], n: &[u8]) -> Option<usize> {
    h.windows(n.len()).position(|w| w == n)
}

/// Parsed request target.
#[derive(Debug, PartialEq)]
pub enum Target {
    Connect {
        host: String,
        port: u16,
    },
    Http {
        host: String,
        port: u16,
        origin_form: String,
    },
}

/// Parse the request line. Pure (unit-tested).
pub fn parse_target(method: &str, target: &str) -> Option<Target> {
    if method.eq_ignore_ascii_case("CONNECT") {
        let (host, port) = split_host_port(target)?;
        return Some(Target::Connect { host, port });
    }
    let rest = target
        .strip_prefix("http://")
        .or_else(|| target.strip_prefix("HTTP://"))?;
    let end = rest.find(['/', '?']).unwrap_or(rest.len());
    let authority = &rest[..end];
    if authority.contains('@') {
        return None;
    }
    let path = &rest[end..];
    let origin_form = if path.is_empty() {
        "/".to_string()
    } else if path.starts_with('?') {
        format!("/{path}")
    } else {
        path.to_string()
    };
    let (host, port) = match split_host_port(authority) {
        Some(hp) => hp,
        None => (authority.trim_matches(['[', ']']).to_string(), 80),
    };
    if host.is_empty() {
        return None;
    }
    Some(Target::Http {
        host,
        port,
        origin_form,
    })
}

fn split_host_port(a: &str) -> Option<(String, u16)> {
    let (host, port) = if let Some(inner) = a.strip_prefix('[') {
        let close = inner.find(']')?;
        let port = inner[close + 1..].strip_prefix(':')?;
        (&inner[..close], port)
    } else {
        let i = a.rfind(':')?;
        (&a[..i], &a[i + 1..])
    };
    let port: u16 = port.parse().ok()?;
    if host.is_empty() || port == 0 {
        return None;
    }
    Some((host.to_string(), port))
}

/// Resolve and keep only allowed addresses (in resolver order).
pub fn resolve_allowed(
    host: &str,
    port: u16,
    allow: AllowIp,
    check_names: bool,
) -> Result<Vec<SocketAddr>, &'static str> {
    if check_names {
        if let Verdict::Block(_) = check_host(host) {
            // Literal IPs and local names are judged without DNS.
            if host.parse::<IpAddr>().is_err() || !allow(host.parse().unwrap()) {
                return Err("destination not allowed");
            }
        }
    }
    let addrs: Vec<SocketAddr> = (host.trim_matches(['[', ']']), port)
        .to_socket_addrs()
        .map_err(|_| "cannot resolve")?
        .collect();
    let ok: Vec<SocketAddr> = addrs.into_iter().filter(|a| allow(a.ip())).collect();
    if ok.is_empty() {
        Err("destination not allowed")
    } else {
        Ok(ok)
    }
}

fn connect_first(addrs: &[SocketAddr]) -> io::Result<TcpStream> {
    let mut last = io::Error::other("no address");
    for a in addrs {
        match TcpStream::connect_timeout(a, CONNECT_TIMEOUT) {
            Ok(s) => return Ok(s),
            Err(e) => last = e,
        }
    }
    Err(last)
}

/// Header block for the upstream: request line in origin-form, hop-by-hop
/// proxy headers removed, `Connection: close` (one request per upstream
/// connection, so a kept-alive client can't reuse it for another host). A
/// plain `ws://` handshake keeps `Connection: Upgrade` instead (RFC 6455), or
/// the server would refuse to switch protocols; the upgraded stream stays on
/// this one host.
pub fn rewrite_head(head: &str, method: &str, origin_form: &str, version: &str) -> String {
    let mut out = format!("{method} {origin_form} {version}\r\n");
    let mut websocket = false;
    for line in head.split("\r\n").skip(1) {
        if let Some((n, v)) = line.split_once(':') {
            if n.trim().eq_ignore_ascii_case("upgrade")
                && v.trim().eq_ignore_ascii_case("websocket")
            {
                websocket = true;
            }
        }
    }
    for line in head.split("\r\n").skip(1) {
        if line.is_empty() {
            continue;
        }
        let name = line
            .split(':')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        if matches!(
            name.as_str(),
            "proxy-connection" | "proxy-authorization" | "connection" | "keep-alive"
        ) {
            continue;
        }
        out.push_str(line);
        out.push_str("\r\n");
    }
    out.push_str(if websocket {
        "Connection: Upgrade\r\n\r\n"
    } else {
        "Connection: close\r\n\r\n"
    });
    out
}

fn pipe(client: TcpStream, upstream: TcpStream) {
    let (Ok(mut c2), Ok(mut u2)) = (client.try_clone(), upstream.try_clone()) else {
        return;
    };
    let t = thread::spawn(move || {
        let mut c = client;
        let mut u = upstream;
        let _ = io::copy(&mut c, &mut u);
        let _ = u.shutdown(Shutdown::Write);
    });
    let _ = io::copy(&mut u2, &mut c2);
    let _ = c2.shutdown(Shutdown::Write);
    let _ = t.join();
}

fn handle(mut client: TcpStream, allow: AllowIp, check_names: bool) -> io::Result<()> {
    client.set_read_timeout(Some(IO_TIMEOUT))?;
    client.set_write_timeout(Some(IO_TIMEOUT))?;
    let (head, rest) = match read_head(&mut client) {
        Ok(x) => x,
        Err(_) => return respond(&client, 400, "bad request"),
    };
    let head = String::from_utf8_lossy(&head).to_string();
    let first = head.split("\r\n").next().unwrap_or("");
    let mut parts = first.split(' ');
    let (method, target, version) = (
        parts.next().unwrap_or(""),
        parts.next().unwrap_or(""),
        parts.next().unwrap_or(""),
    );
    if !version.starts_with("HTTP/1.") {
        return respond(&client, 400, "unsupported request");
    }
    let Some(t) = parse_target(method, target) else {
        return respond(&client, 400, "unsupported request");
    };
    let (host, port) = match &t {
        Target::Connect { host, port } | Target::Http { host, port, .. } => (host.clone(), *port),
    };
    let addrs = match resolve_allowed(&host, port, allow, check_names) {
        Ok(a) => a,
        Err(why) => {
            note_block(&host);
            return respond(&client, 403, why);
        }
    };
    let mut upstream = match connect_first(&addrs) {
        Ok(u) => u,
        Err(_) => return respond(&client, 502, "upstream unreachable"),
    };
    upstream.set_read_timeout(Some(Duration::from_secs(300)))?;
    client.set_read_timeout(Some(Duration::from_secs(300)))?;
    match t {
        Target::Connect { .. } => {
            client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")?;
            if !rest.is_empty() {
                upstream.write_all(&rest)?;
            }
        }
        Target::Http { origin_form, .. } => {
            upstream.write_all(rewrite_head(&head, method, &origin_form, version).as_bytes())?;
            if !rest.is_empty() {
                upstream.write_all(&rest)?;
            }
        }
    }
    pipe(client, upstream);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::is_public_ip;
    use std::io::BufRead;

    fn allow_all(_: IpAddr) -> bool {
        true
    }

    #[test]
    fn plain_websocket_handshake_keeps_upgrade() {
        let head = "GET http://example.com/ws HTTP/1.1\r\nHost: example.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n\r\n";
        let out = rewrite_head(head, "GET", "/ws", "HTTP/1.1");
        assert!(out.contains("Upgrade: websocket\r\n"));
        assert!(out.ends_with("Connection: Upgrade\r\n\r\n"));
        assert!(!out.contains("Connection: close"));
    }

    #[test]
    fn targets() {
        assert_eq!(
            parse_target("CONNECT", "example.com:443"),
            Some(Target::Connect {
                host: "example.com".into(),
                port: 443
            })
        );
        assert_eq!(
            parse_target("CONNECT", "[::1]:443"),
            Some(Target::Connect {
                host: "::1".into(),
                port: 443
            })
        );
        assert_eq!(
            parse_target("GET", "http://example.com/a?b"),
            Some(Target::Http {
                host: "example.com".into(),
                port: 80,
                origin_form: "/a?b".into()
            })
        );
        assert_eq!(
            parse_target("GET", "http://example.com:8080"),
            Some(Target::Http {
                host: "example.com".into(),
                port: 8080,
                origin_form: "/".into()
            })
        );
        assert_eq!(
            parse_target("GET", "http://example.com?x"),
            Some(Target::Http {
                host: "example.com".into(),
                port: 80,
                origin_form: "/?x".into()
            })
        );
        for (m, t) in [
            ("GET", "/relative"),
            ("GET", "https://example.com/"),
            ("GET", "http://u:p@example.com/"),
            ("CONNECT", "example.com"),
            ("CONNECT", "example.com:0"),
            ("CONNECT", ":443"),
            ("GET", "http:///x"),
        ] {
            assert_eq!(parse_target(m, t), None, "{m} {t}");
        }
    }

    #[test]
    fn rewrite_strips_proxy_headers() {
        let head = "GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\nProxy-Connection: keep-alive\r\nProxy-Authorization: x\r\nConnection: keep-alive\r\nAccept: */*\r\n\r\n";
        let out = rewrite_head(head, "GET", "/x", "HTTP/1.1");
        assert_eq!(
            out,
            "GET /x HTTP/1.1\r\nHost: example.com\r\nAccept: */*\r\nConnection: close\r\n\r\n"
        );
    }

    #[test]
    fn resolve_blocks_local_destinations() {
        for h in [
            "127.0.0.1",
            "localhost",
            "::1",
            "10.0.0.1",
            "169.254.169.254",
            "nas.local",
            "100.64.0.1",
        ] {
            assert!(resolve_allowed(h, 80, is_public_ip, true).is_err(), "{h}");
        }
        // A literal public IP needs no DNS.
        assert_eq!(
            resolve_allowed("8.8.8.8", 443, is_public_ip, true).unwrap()[0].to_string(),
            "8.8.8.8:443"
        );
    }

    /// Simulates DNS rebinding: the name passes the URL policy but resolves
    /// to loopback; the post-DNS check must refuse it.
    #[test]
    fn post_dns_check_refuses_loopback_answers() {
        // "localhost" is the one name that resolves to loopback everywhere;
        // with name checks off we exercise only the post-DNS filter.
        assert!(resolve_allowed("localhost", 80, is_public_ip, false).is_err());
    }

    fn upstream_echo() -> SocketAddr {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let a = l.local_addr().unwrap();
        thread::spawn(move || {
            for s in l.incoming().take(4) {
                let mut s = s.unwrap();
                let mut r = io::BufReader::new(s.try_clone().unwrap());
                let mut first = String::new();
                r.read_line(&mut first).unwrap();
                let body = format!("echo:{}", first.trim());
                let _ = write!(
                    s,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        a
    }

    fn roundtrip(proxy: SocketAddr, req: &str) -> String {
        let mut s = TcpStream::connect(proxy).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        s.write_all(req.as_bytes()).unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    }

    #[test]
    fn default_policy_refuses_loopback_through_the_proxy() {
        let up = upstream_echo();
        let p = spawn(is_public_ip).unwrap();
        let r = roundtrip(
            p.addr,
            &format!("GET http://{up}/x HTTP/1.1\r\nHost: {up}\r\n\r\n"),
        );
        assert!(r.starts_with("HTTP/1.1 403"), "{r}");
        let r = roundtrip(
            p.addr,
            &format!("CONNECT {up} HTTP/1.1\r\nHost: {up}\r\n\r\n"),
        );
        assert!(r.starts_with("HTTP/1.1 403"), "{r}");
        let r = roundtrip(
            p.addr,
            &format!("GET http://localhost:{}/x HTTP/1.1\r\n\r\n", up.port()),
        );
        assert!(r.starts_with("HTTP/1.1 403"), "{r}");
        p.stop();
    }

    #[test]
    fn a_refused_host_is_remembered_for_the_ui() {
        let up = upstream_echo();
        let p = spawn(is_public_ip).unwrap();
        let r = roundtrip(
            p.addr,
            &format!("GET http://localhost:{}/x HTTP/1.1\r\n\r\n", up.port()),
        );
        assert!(r.starts_with("HTTP/1.1 403"), "{r}");
        assert!(recent_blocks(Duration::from_secs(30)).contains(&"localhost".to_string()));
        assert!(!recent_blocks(Duration::ZERO).contains(&"localhost".to_string()));
        p.stop();
    }

    #[test]
    fn plain_http_is_forwarded_in_origin_form() {
        let up = upstream_echo();
        let p = spawn_with(allow_all, false).unwrap();
        let r = roundtrip(p.addr, &format!("GET http://{up}/hello?q=1 HTTP/1.1\r\nHost: {up}\r\nProxy-Connection: keep-alive\r\n\r\n"));
        assert!(r.starts_with("HTTP/1.1 200"), "{r}");
        assert!(r.ends_with("echo:GET /hello?q=1 HTTP/1.1"), "{r}");
        p.stop();
    }

    #[test]
    fn connect_tunnels_bytes() {
        let up = upstream_echo();
        let p = spawn_with(allow_all, false).unwrap();
        let r = roundtrip(
            p.addr,
            &format!("CONNECT {up} HTTP/1.1\r\nHost: {up}\r\n\r\nPING / HTTP/1.1\r\n\r\n"),
        );
        assert!(
            r.starts_with("HTTP/1.1 200 Connection Established\r\n\r\n"),
            "{r}"
        );
        assert!(r.ends_with("echo:PING / HTTP/1.1"), "{r}");
        p.stop();
    }

    #[test]
    fn garbage_and_origin_form_get_400() {
        let p = spawn(is_public_ip).unwrap();
        assert!(roundtrip(p.addr, "GET / HTTP/1.1\r\nHost: x\r\n\r\n").starts_with("HTTP/1.1 400"));
        assert!(roundtrip(p.addr, "hello\r\n\r\n").starts_with("HTTP/1.1 400"));
        assert!(
            roundtrip(p.addr, "GET https://example.com/ HTTP/1.1\r\n\r\n")
                .starts_with("HTTP/1.1 400")
        );
        p.stop();
    }

    #[test]
    fn url_is_loopback_http() {
        let p = spawn(is_public_ip).unwrap();
        assert!(p.url().starts_with("http://127.0.0.1:"));
        p.stop();
    }
}
