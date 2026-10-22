use conductloop_url_policy::effects::{classify, code, Action, Element, Page};

#[test]
fn shared_vectors() {
    let src = include_str!("effect_vectors.txt");
    let mut n = 0;
    for line in src
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
    {
        let (lhs, expected) = line.split_once("=>").expect("=>");
        let f: Vec<&str> = lhs.split('|').map(str::trim).collect();
        assert_eq!(f.len(), 12, "{line}");
        let action = match f[0] {
            "click" => Action::Click,
            "type" => Action::Type { submit: false },
            "type+submit" => Action::Type { submit: true },
            other => panic!("action {other}"),
        };
        let el = Element {
            tag: f[1],
            typ: f[2],
            role: f[3],
            name: f[4],
            autocomplete: f[5],
            in_form: f[6] == "1",
            href: if f[7] == "-" || f[7].is_empty() {
                None
            } else {
                Some(f[7])
            },
            payment: f[8] == "1",
        };
        let page = Page {
            host: f[9],
            has_password: f[10] == "1",
            has_payment: f[11] == "1",
        };
        assert_eq!(
            code(classify(action, &el, &page)),
            expected.trim(),
            "{line}"
        );
        n += 1;
    }
    assert!(n >= 20);
}
