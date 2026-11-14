use conductloop_url_policy::sites::registrable_domain;

#[test]
fn shared_site_vectors() {
    let mut n = 0;
    for line in include_str!("site_vectors.txt")
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
    {
        let (host, want) = line.split_once("=>").expect("=>");
        assert_eq!(registrable_domain(host.trim()), want.trim(), "{line}");
        n += 1;
    }
    assert!(n >= 20);
}
