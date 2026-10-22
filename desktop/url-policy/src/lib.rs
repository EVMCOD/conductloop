//! Destination policy for ConductLoop's in-app browser (design doc
//! 2026-10-21, §1.3, decision B6).
//!
//! Browser tabs show untrusted pages. They must never reach the local API
//! (127.0.0.1:7777), other local services (Ollama, dev servers), the LAN
//! (router, NAS), link-local/metadata addresses or Tailscale (100.64/10).
//!
//! Two entry points:
//! * [`check_navigation`] — for `on_navigation` / `on_new_window` and the
//!   agent's `navigate` tool. Sees only the URL (no DNS), so it is a cheap
//!   first layer: scheme allow-list + literal hosts/IPs.
//! * [`check_ip`] — for the filtering proxy (PR 5), applied **after** DNS
//!   resolution, which is what defeats DNS rebinding.
//!
//! No dependencies on purpose: it is tested on Linux CI without WebKit.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

pub mod effects;
pub mod proxy;
pub mod sites;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// Scheme other than http/https (or about:blank).
    Scheme,
    /// Malformed or empty authority.
    BadUrl,
    /// `user:pass@` in the URL (no credentials in v1; also a phishing trick).
    Userinfo,
    /// localhost, *.localhost, *.local, *.internal, *.home.arpa …
    LocalName,
    /// Loopback IP.
    Loopback,
    /// RFC1918, ULA, CGNAT/Tailscale, link-local …
    Private,
    /// 0/8, multicast, documentation, benchmarking, reserved …
    Reserved,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Allow,
    Block(Reason),
}

impl Verdict {
    pub fn allowed(self) -> bool {
        self == Verdict::Allow
    }
}

/// Hostname suffixes that only make sense on a local network.
const LOCAL_SUFFIXES: &[&str] = &["localhost", "local", "internal", "home.arpa", "localdomain"];

/// Policy for a top-level navigation (or a new-window request).
pub fn check_navigation(url: &str) -> Verdict {
    let url = url.trim();
    let Some(colon) = url.find(':') else {
        return Verdict::Block(Reason::BadUrl);
    };
    let scheme = url[..colon].to_ascii_lowercase();
    let rest = &url[colon + 1..];
    match scheme.as_str() {
        "about" => {
            if rest.eq_ignore_ascii_case("blank") {
                Verdict::Allow
            } else {
                Verdict::Block(Reason::Scheme)
            }
        }
        "http" | "https" => {
            let Some(after) = rest.strip_prefix("//") else {
                return Verdict::Block(Reason::BadUrl);
            };
            let end = after.find(['/', '?', '#', '\\']).unwrap_or(after.len());
            let authority = &after[..end];
            if authority.contains('@') {
                return Verdict::Block(Reason::Userinfo);
            }
            match split_host(authority) {
                Some(host) => check_host(host),
                None => Verdict::Block(Reason::BadUrl),
            }
        }
        _ => Verdict::Block(Reason::Scheme),
    }
}

/// Host part of `host[:port]` / `[v6]:port`, without brackets.
fn split_host(authority: &str) -> Option<&str> {
    if authority.is_empty() {
        return None;
    }
    if let Some(inner) = authority.strip_prefix('[') {
        let close = inner.find(']')?;
        let tail = &inner[close + 1..];
        if !(tail.is_empty() || tail.starts_with(':')) {
            return None;
        }
        return Some(&inner[..close]);
    }
    let host = match authority.rfind(':') {
        Some(i) => &authority[..i],
        None => authority,
    };
    if host.is_empty() || host.contains(':') {
        None
    } else {
        Some(host)
    }
}

/// Policy for a host as written in a URL (name or IP literal), without DNS.
pub fn check_host(host: &str) -> Verdict {
    let h = host.trim().trim_end_matches('.').to_ascii_lowercase();
    let h = h
        .strip_prefix('[')
        .and_then(|x| x.strip_suffix(']'))
        .unwrap_or(&h)
        .to_string();
    if h.is_empty() {
        return Verdict::Block(Reason::BadUrl);
    }
    if let Ok(v6) = h.parse::<Ipv6Addr>() {
        return check_ip(IpAddr::V6(v6));
    }
    if let Some(v4) = parse_whatwg_ipv4(&h) {
        return check_ip(IpAddr::V4(v4));
    }
    if h.contains('%') || h.contains(' ') {
        return Verdict::Block(Reason::BadUrl);
    }
    for suf in LOCAL_SUFFIXES {
        if h == *suf || h.ends_with(&format!(".{suf}")) {
            return Verdict::Block(Reason::LocalName);
        }
    }
    // Single-label names (e.g. "router", "nas") resolve via the local search
    // domain / mDNS: treat as local.
    if !h.contains('.') {
        return Verdict::Block(Reason::LocalName);
    }
    Verdict::Allow
}

/// WHATWG IPv4 host parser: 1–4 parts, each decimal, 0x-hex or 0-octal; the
/// last part fills the remaining bytes ("127.1", "2130706433", "0x7f.1").
/// Returns None if the host is not an IPv4 literal (then it is a name).
pub fn parse_whatwg_ipv4(h: &str) -> Option<Ipv4Addr> {
    let h = h.strip_suffix('.').unwrap_or(h);
    let parts: Vec<&str> = h.split('.').collect();
    if parts.is_empty() || parts.len() > 4 {
        return None;
    }
    // Per WHATWG, it is only an IPv4 literal if the last part is numeric.
    let mut nums = Vec::with_capacity(parts.len());
    for p in &parts {
        nums.push(parse_ipv4_number(p)?);
    }
    let (last, init) = nums.split_last()?;
    if init.iter().any(|&n| n > 255) {
        return None;
    }
    let width = 8 * (5 - nums.len() as u32);
    if width < 64 && *last >= (1u64 << width) {
        return None;
    }
    let mut v: u64 = *last;
    for (i, n) in init.iter().enumerate() {
        v += n << (8 * (3 - i as u32));
    }
    if v > u32::MAX as u64 {
        return None;
    }
    Some(Ipv4Addr::from(v as u32))
}

fn parse_ipv4_number(p: &str) -> Option<u64> {
    if p.is_empty() {
        return None;
    }
    let (digits, radix) = if let Some(x) = p.strip_prefix("0x").or_else(|| p.strip_prefix("0X")) {
        (x, 16)
    } else if p.len() > 1 && p.starts_with('0') {
        (&p[1..], 8)
    } else {
        (p, 10)
    };
    if digits.is_empty() {
        return Some(0); // "0x" == 0 per WHATWG
    }
    if digits.len() > 20 {
        return None;
    }
    u64::from_str_radix(digits, radix).ok()
}

/// Policy for a concrete destination address (after DNS).
pub fn check_ip(ip: IpAddr) -> Verdict {
    match ip {
        IpAddr::V4(v4) => check_v4(v4),
        IpAddr::V6(v6) => check_v6(v6),
    }
}

/// Convenience for the proxy.
pub fn is_public_ip(ip: IpAddr) -> bool {
    check_ip(ip).allowed()
}

fn in_v4(ip: Ipv4Addr, net: [u8; 4], prefix: u32) -> bool {
    let mask = if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix)
    };
    (u32::from(ip) & mask) == (u32::from(Ipv4Addr::from(net)) & mask)
}

fn check_v4(ip: Ipv4Addr) -> Verdict {
    const LOOPBACK: &[([u8; 4], u32)] = &[([127, 0, 0, 0], 8)];
    const PRIVATE: &[([u8; 4], u32)] = &[
        ([10, 0, 0, 0], 8),
        ([172, 16, 0, 0], 12),
        ([192, 168, 0, 0], 16),
        ([100, 64, 0, 0], 10),  // CGNAT, Tailscale
        ([169, 254, 0, 0], 16), // link-local, cloud metadata
    ];
    const RESERVED: &[([u8; 4], u32)] = &[
        ([0, 0, 0, 0], 8),
        ([192, 0, 0, 0], 24),
        ([192, 0, 2, 0], 24),
        ([192, 88, 99, 0], 24),
        ([198, 18, 0, 0], 15),
        ([198, 51, 100, 0], 24),
        ([203, 0, 113, 0], 24),
        ([224, 0, 0, 0], 4), // multicast
        ([240, 0, 0, 0], 4), // reserved + broadcast
    ];
    if LOOPBACK.iter().any(|(n, p)| in_v4(ip, *n, *p)) {
        return Verdict::Block(Reason::Loopback);
    }
    if PRIVATE.iter().any(|(n, p)| in_v4(ip, *n, *p)) {
        return Verdict::Block(Reason::Private);
    }
    if RESERVED.iter().any(|(n, p)| in_v4(ip, *n, *p)) {
        return Verdict::Block(Reason::Reserved);
    }
    Verdict::Allow
}

fn check_v6(ip: Ipv6Addr) -> Verdict {
    let s = ip.segments();
    if ip.is_loopback() {
        return Verdict::Block(Reason::Loopback);
    }
    if ip.is_unspecified() {
        return Verdict::Block(Reason::Reserved);
    }
    // Embedded IPv4: ::ffff:a.b.c.d (mapped), ::a.b.c.d (compatible),
    // 64:ff9b::/96 (NAT64), 2002::/16 (6to4).
    let v4_tail = Ipv4Addr::new((s[6] >> 8) as u8, s[6] as u8, (s[7] >> 8) as u8, s[7] as u8);
    if s[0..5] == [0, 0, 0, 0, 0] && (s[5] == 0xffff || s[5] == 0) {
        return check_v4(v4_tail);
    }
    if s[0] == 0x64 && s[1] == 0xff9b && s[2..6] == [0, 0, 0, 0] {
        return check_v4(v4_tail);
    }
    // 64:ff9b:1::/48 (RFC 8215 local-use NAT64): the operator picks where the
    // IPv4 address sits, so it cannot be decoded reliably — block the prefix.
    if s[0] == 0x64 && s[1] == 0xff9b && s[2] == 1 {
        return Verdict::Block(Reason::Reserved);
    }
    if s[0] == 0x2002 {
        let v4 = Ipv4Addr::new((s[1] >> 8) as u8, s[1] as u8, (s[2] >> 8) as u8, s[2] as u8);
        return check_v4(v4);
    }
    if (s[0] & 0xfe00) == 0xfc00 {
        return Verdict::Block(Reason::Private); // ULA fc00::/7
    }
    if (s[0] & 0xffc0) == 0xfe80 || (s[0] & 0xffc0) == 0xfec0 {
        return Verdict::Block(Reason::Private); // link-local, old site-local
    }
    if (s[0] & 0xff00) == 0xff00 {
        return Verdict::Block(Reason::Reserved); // multicast
    }
    if s[0] == 0x2001 && s[1] == 0x0db8 {
        return Verdict::Block(Reason::Reserved); // documentation
    }
    if s[0] == 0x0100 && s[1..4] == [0, 0, 0] {
        return Verdict::Block(Reason::Reserved); // discard 100::/64
    }
    Verdict::Allow
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blocked(url: &str) -> Reason {
        match check_navigation(url) {
            Verdict::Block(r) => r,
            Verdict::Allow => panic!("expected {url} to be blocked"),
        }
    }

    #[test]
    fn public_sites_allowed() {
        for u in [
            "https://example.com/",
            "https://www.wikipedia.org/wiki/Tauri",
            "http://example.com:8080/x?y#z",
            "HTTPS://EXAMPLE.COM",
            "https://example.com.",
            "https://[2606:4700:4700::1111]/",
            "https://8.8.8.8/",
            "about:blank",
            "https://sub.localhost-tools.com/",
            "https://notlocal.dev/",
        ] {
            assert_eq!(check_navigation(u), Verdict::Allow, "{u}");
        }
    }

    #[test]
    fn schemes_blocked() {
        for u in [
            "file:///etc/passwd",
            "tauri://localhost/index.html",
            "ipc://localhost/x",
            "data:text/html,<script>1</script>",
            "javascript:alert(1)",
            "blob:https://example.com/uuid",
            "ftp://example.com/",
            "about:srcdoc",
            "about:config",
            "ws://example.com/",
            "view-source:https://example.com",
        ] {
            assert_eq!(blocked(u), Reason::Scheme, "{u}");
        }
    }

    #[test]
    fn local_api_and_loopback_blocked() {
        for u in [
            "http://localhost:7777/",
            "http://LOCALHOST:7777/api/projects",
            "http://localhost./",
            "http://app.localhost/",
            "http://127.0.0.1:7777/",
            "http://127.1/",
            "http://127.0.0.1.:7777/",
            "http://2130706433/",
            "http://0x7f000001/",
            "http://0x7f.1/",
            "http://0177.0.0.1/",
            "http://[::1]:7777/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:7f00:1]/",
            "http://[64:ff9b::7f00:1]/",
            "http://[2002:7f00:1::]/",
        ] {
            let r = blocked(u);
            assert!(
                matches!(r, Reason::Loopback | Reason::LocalName),
                "{u}: {r:?}"
            );
        }
    }

    #[test]
    fn lan_tailscale_linklocal_blocked() {
        for u in [
            "http://192.168.1.1/",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://172.31.255.255/",
            "http://100.64.0.1/",
            "http://100.101.102.103/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[fd7a:115c:a1e0::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:192.168.0.1]/",
            "http://nas.local/",
            "http://printer.home.arpa/",
            "http://db.internal/",
            "http://router/",
            "http://router.localdomain/",
        ] {
            let r = blocked(u);
            assert!(
                matches!(r, Reason::Private | Reason::LocalName),
                "{u}: {r:?}"
            );
        }
    }

    #[test]
    fn reserved_blocked() {
        for u in [
            "http://0.0.0.0:7777/",
            "http://0/",
            "http://[::]/",
            "http://224.0.0.1/",
            "http://255.255.255.255/",
            "http://192.0.2.1/",
            "http://[2001:db8::1]/",
            "http://[ff02::1]/",
        ] {
            let r = blocked(u);
            assert!(
                matches!(r, Reason::Reserved | Reason::Loopback),
                "{u}: {r:?}"
            );
        }
    }

    #[test]
    fn userinfo_and_malformed_blocked() {
        assert_eq!(blocked("https://user:pass@example.com/"), Reason::Userinfo);
        assert_eq!(blocked("https://example.com@127.0.0.1/"), Reason::Userinfo);
        assert_eq!(blocked("http:///x"), Reason::BadUrl);
        assert_eq!(blocked("http:example.com"), Reason::BadUrl);
        assert_eq!(blocked("http://[::1/"), Reason::BadUrl);
        assert_eq!(blocked("http://[::1]x/"), Reason::BadUrl);
        assert_eq!(blocked("nonsense"), Reason::BadUrl);
        assert_eq!(blocked("http://exa mple.com/"), Reason::BadUrl);
    }

    #[test]
    fn backslash_is_a_path_separator() {
        // WHATWG treats "\" like "/" for special schemes.
        assert_eq!(blocked("http://127.0.0.1\\@example.com/"), Reason::Loopback);
    }

    #[test]
    fn whatwg_ipv4_forms() {
        assert_eq!(
            parse_whatwg_ipv4("127.1"),
            Some(Ipv4Addr::new(127, 0, 0, 1))
        );
        assert_eq!(
            parse_whatwg_ipv4("2130706433"),
            Some(Ipv4Addr::new(127, 0, 0, 1))
        );
        assert_eq!(
            parse_whatwg_ipv4("0x7f.0.0.1"),
            Some(Ipv4Addr::new(127, 0, 0, 1))
        );
        assert_eq!(
            parse_whatwg_ipv4("0300.0250.1.1"),
            Some(Ipv4Addr::new(192, 168, 1, 1))
        );
        assert_eq!(parse_whatwg_ipv4("10.1"), Some(Ipv4Addr::new(10, 0, 0, 1)));
        assert_eq!(parse_whatwg_ipv4("example.com"), None);
        assert_eq!(parse_whatwg_ipv4("1.2.3.4.5"), None);
        assert_eq!(parse_whatwg_ipv4("256.1.1.1"), None);
        assert_eq!(parse_whatwg_ipv4("4294967296"), None);
        assert_eq!(parse_whatwg_ipv4("1.2.3.256"), None);
    }

    #[test]
    fn check_ip_for_proxy() {
        assert!(is_public_ip("93.184.216.34".parse().unwrap()));
        assert!(is_public_ip("2606:4700::6810:84e5".parse().unwrap()));
        for ip in [
            "127.0.0.1",
            "::1",
            "10.1.2.3",
            "100.100.100.100",
            "169.254.1.1",
            "fd00::1",
            "::ffff:10.0.0.1",
            "0.0.0.0",
            "64:ff9b::a00:1",
            "64:ff9b:1::a00:1", // RFC 8215 local-use NAT64
            "64:ff9b:1:ffff::7f00:1",
        ] {
            assert!(!is_public_ip(ip.parse().unwrap()), "{ip}");
        }
    }
}
