//! Registrable domains ("eTLD+1") for the agent's new-site confirmation
//! (browser design B10, follow-up to PR 9). Mirrored in Python
//! (`ops_dashboard/sessions/browser_effects.py`); both are tested against
//! `tests/site_vectors.txt`.
//!
//! This is NOT the full Public Suffix List: an embedded list of the common
//! multi-label public suffixes (ccTLD second levels and shared hosting such
//! as `github.io`), plus a heuristic for unknown ccTLDs (`co.`/`com.`/`org.`…
//! under a 2-letter TLD counts as a suffix). Errors on unlisted suffixes make
//! two unrelated sites look like one; the list is biased to avoid that for
//! the hosts where it matters most (shared hosting).

/// Multi-label public suffixes (lower-case, no leading dot).
pub const MULTI_LABEL_SUFFIXES: &[&str] = &[
    // ccTLD second levels
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "me.uk",
    "ltd.uk",
    "plc.uk",
    "net.uk",
    "sch.uk",
    "nhs.uk",
    "police.uk",
    "com.au",
    "net.au",
    "org.au",
    "edu.au",
    "gov.au",
    "asn.au",
    "id.au",
    "co.nz",
    "org.nz",
    "net.nz",
    "govt.nz",
    "ac.nz",
    "co.jp",
    "ne.jp",
    "or.jp",
    "ac.jp",
    "go.jp",
    "gr.jp",
    "co.kr",
    "or.kr",
    "ne.kr",
    "go.kr",
    "ac.kr",
    "com.br",
    "net.br",
    "org.br",
    "gov.br",
    "com.mx",
    "org.mx",
    "gob.mx",
    "com.ar",
    "gob.ar",
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "com.tw",
    "org.tw",
    "com.hk",
    "org.hk",
    "com.sg",
    "edu.sg",
    "gov.sg",
    "co.in",
    "net.in",
    "org.in",
    "gov.in",
    "ac.in",
    "co.za",
    "org.za",
    "gov.za",
    "com.tr",
    "org.tr",
    "gov.tr",
    "com.es",
    "org.es",
    "gob.es",
    "nom.es",
    "edu.es",
    "co.il",
    "org.il",
    "ac.il",
    "gov.il",
    "com.pl",
    "net.pl",
    "org.pl",
    "com.pt",
    "com.co",
    "com.pe",
    "com.ve",
    "com.ec",
    "com.uy",
    "com.do",
    "com.gt",
    "com.bo",
    "com.py",
    "com.sv",
    "com.ni",
    "com.pa",
    "com.cu",
    // shared hosting: every customer is its own site
    "github.io",
    "gitlab.io",
    "herokuapp.com",
    "vercel.app",
    "netlify.app",
    "pages.dev",
    "workers.dev",
    "web.app",
    "firebaseapp.com",
    "appspot.com",
    "blogspot.com",
    "azurewebsites.net",
    "cloudfront.net",
    "s3.amazonaws.com",
    "fly.dev",
    "onrender.com",
    "glitch.me",
    "repl.co",
    "ngrok.io",
    "ngrok-free.app",
    "trycloudflare.com",
    "wordpress.com",
    "substack.com",
    "notion.site",
    "webflow.io",
    "framer.app",
    "surge.sh",
    "neocities.org",
];

const SECOND_LEVEL_WORDS: &[&str] = &[
    "co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nom", "mil",
];

fn is_ip_literal(h: &str) -> bool {
    h.contains(':')
        || (!h.is_empty()
            && h.split('.')
                .all(|l| !l.is_empty() && l.chars().all(|c| c.is_ascii_digit())))
}

/// Registrable domain of `host` (lower-cased, trailing dot removed). IP
/// literals and single-label hosts are returned as they are.
pub fn registrable_domain(host: &str) -> String {
    let h = host.trim().trim_end_matches('.').to_ascii_lowercase();
    let h = h.trim_start_matches('[').trim_end_matches(']').to_string();
    if is_ip_literal(&h) {
        return h;
    }
    let labels: Vec<&str> = h.split('.').filter(|l| !l.is_empty()).collect();
    if labels.len() <= 2 {
        return labels.join(".");
    }
    let n = labels.len();
    let mut suffix_len = 1;
    for s in MULTI_LABEL_SUFFIXES {
        let k = s.split('.').count();
        if k > suffix_len && k < n && labels[n - k..].join(".") == *s {
            suffix_len = k;
        }
    }
    if suffix_len == 1 && labels[n - 1].len() == 2 && SECOND_LEVEL_WORDS.contains(&labels[n - 2]) {
        suffix_len = 2;
    }
    if n == suffix_len {
        return labels.join(".");
    }
    labels[n - suffix_len - 1..].join(".")
}

/// Host of an http(s) URL, or None for any other scheme (about:blank…).
pub fn http_host(url: &str) -> Option<String> {
    let lower = url.trim().to_ascii_lowercase();
    let rest = lower
        .strip_prefix("https://")
        .or_else(|| lower.strip_prefix("http://"))?;
    let auth = rest.split(['/', '?', '#']).next()?;
    let auth = auth.rsplit('@').next()?;
    let host = if let Some(v6) = auth.strip_prefix('[') {
        v6.split(']').next()?.to_string()
    } else {
        auth.split(':').next()?.to_string()
    };
    (!host.is_empty()).then_some(host)
}

/// The new-site gate: `Some(site)` when an agent navigation to `url` needs
/// the user's confirmation because `site` was never opened in the session;
/// `None` when it may proceed (known site, or not an http(s) URL — those are
/// the URL policy's business).
pub fn nav_needs_confirm(known: &[String], url: &str) -> Option<String> {
    let site = registrable_domain(&http_host(url)?);
    (!known.contains(&site)).then_some(site)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gate() {
        let known = vec!["example.com".to_string()];
        assert_eq!(nav_needs_confirm(&known, "https://www.example.com/x"), None);
        assert_eq!(
            nav_needs_confirm(&known, "https://a.b.example.com:8443/"),
            None
        );
        assert_eq!(nav_needs_confirm(&known, "about:blank"), None);
        assert_eq!(
            nav_needs_confirm(&known, "https://attacker.example/c?d=1"),
            Some("attacker.example".into())
        );
        assert_eq!(
            nav_needs_confirm(&known, "https://example.com.evil.net/"),
            Some("evil.net".into())
        );
        assert_eq!(
            nav_needs_confirm(&known, "https://user@evil.net/"),
            Some("evil.net".into())
        );
    }
}
