//! Effect classifier for the agent's `click` / `type` (browser design PR 9,
//! §3.3, decision B10). Deterministic — the model never decides — and
//! conservative: when in doubt, confirm.
//!
//! The Python side (`ops_dashboard/sessions/browser_effects.py`) implements
//! the same rules to ask the user *before* calling the bridge; Rust applies
//! them **again** on the element actually found in the page right before
//! acting, in case the backend got it wrong. Both are tested against the same
//! vectors (`tests/effect_vectors.txt`).

/// What the page script reports about the target element.
#[derive(Debug, Default, Clone)]
pub struct Element<'a> {
    pub tag: &'a str,
    pub typ: &'a str,
    pub role: &'a str,
    pub name: &'a str,
    pub autocomplete: &'a str,
    pub in_form: bool,
    pub href: Option<&'a str>,
    pub payment: bool,
}

#[derive(Debug, Default, Clone)]
pub struct Page<'a> {
    pub host: &'a str,
    pub has_password: bool,
    pub has_payment: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Click,
    Type { submit: bool },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Effect {
    Allow,
    Confirm(&'static str),
    Deny(&'static str),
}

/// Verbs of buying, publishing, sending, deleting… (es/en), matched on the
/// accessible name after lower-casing and stripping accents.
pub const EFFECT_VERBS: &[&str] = &[
    "comprar",
    "compra",
    "pagar",
    "pago",
    "pedido",
    "tramitar",
    "reservar",
    "donar",
    "transferir",
    "publicar",
    "enviar",
    "mandar",
    "borrar",
    "eliminar",
    "suscrib",
    "darse de baja",
    "confirmar",
    "registrar",
    "aceptar y pagar",
    "buy",
    "purchase",
    "checkout",
    "check out",
    "order",
    "pay",
    "donate",
    "transfer",
    "book",
    "post",
    "publish",
    "send",
    "submit",
    "delete",
    "remove",
    "subscribe",
    "unsubscribe",
    "confirm",
    "sign up",
    "register",
    "place order",
];

fn fold(c: char) -> char {
    match c {
        'á' | 'à' | 'ä' | 'â' | 'Á' | 'À' | 'Ä' | 'Â' => 'a',
        'é' | 'è' | 'ë' | 'ê' | 'É' | 'È' | 'Ë' | 'Ê' => 'e',
        'í' | 'ì' | 'ï' | 'î' | 'Í' | 'Ì' | 'Ï' | 'Î' => 'i',
        'ó' | 'ò' | 'ö' | 'ô' | 'Ó' | 'Ò' | 'Ö' | 'Ô' => 'o',
        'ú' | 'ù' | 'ü' | 'û' | 'Ú' | 'Ù' | 'Ü' | 'Û' => 'u',
        'ñ' | 'Ñ' => 'n',
        c => c.to_ascii_lowercase(),
    }
}

/// Lower-case, accents folded, non-alphanumerics → single spaces.
pub fn normalize(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut space = true;
    for c in s.chars().map(fold) {
        if c.is_alphanumeric() {
            out.push(c);
            space = false;
        } else if !space {
            out.push(' ');
            space = true;
        }
    }
    out.trim_end().to_string()
}

/// Does `name` contain an effect verb at a word start?
pub fn has_effect_verb(name: &str) -> bool {
    let n = format!(" {}", normalize(name));
    EFFECT_VERBS.iter().any(|v| n.contains(&format!(" {v}")))
}

/// Registrable domain (eTLD+1) of `host`: what "same site" means for
/// cross-domain links and the new-site gate.
pub fn site(host: &str) -> String {
    crate::sites::registrable_domain(host)
}

fn href_host(href: &str) -> Option<&str> {
    let rest = href.split_once("://")?.1;
    let auth = rest.split(['/', '?', '#']).next()?;
    let auth = auth.rsplit('@').next()?;
    if auth.starts_with('[') {
        return auth.split(']').next().map(|h| h.trim_start_matches('['));
    }
    auth.split(':').next()
}

fn is_password(el: &Element) -> bool {
    el.typ.eq_ignore_ascii_case("password")
        || el.autocomplete.to_ascii_lowercase().contains("password")
}

fn is_payment(el: &Element) -> bool {
    el.payment || el.autocomplete.to_ascii_lowercase().starts_with("cc-")
}

pub fn classify(action: Action, el: &Element, page: &Page) -> Effect {
    let tag = el.tag.to_ascii_lowercase();
    let typ = el.typ.to_ascii_lowercase();
    match action {
        Action::Type { submit } => {
            if is_password(el) {
                return Effect::Deny("password_field");
            }
            if !(matches!(tag.as_str(), "input" | "textarea") || el.role == "textbox") {
                return Effect::Deny("not_a_text_field");
            }
            if matches!(
                typ.as_str(),
                "checkbox" | "radio" | "file" | "submit" | "button" | "image" | "hidden"
            ) {
                return Effect::Deny("not_a_text_field");
            }
            if is_payment(el) {
                return Effect::Confirm("payment_field");
            }
            if submit {
                return Effect::Confirm("submit");
            }
            if page.has_password || page.has_payment {
                return Effect::Confirm("sensitive_page");
            }
            Effect::Allow
        }
        Action::Click => {
            if is_password(el) || typ == "file" {
                return Effect::Deny("password_field");
            }
            if page.has_password || page.has_payment || is_payment(el) {
                return Effect::Confirm("sensitive_page");
            }
            let submits = (tag == "button" && (typ.is_empty() || typ == "submit"))
                || (tag == "input" && matches!(typ.as_str(), "submit" | "image"));
            if el.in_form && submits {
                return Effect::Confirm("submit");
            }
            if has_effect_verb(el.name) {
                return Effect::Confirm("effect_verb");
            }
            if tag == "a" {
                match el.href {
                    None => {}
                    Some(h) if !(h.starts_with("http://") || h.starts_with("https://")) => {
                        return Effect::Confirm("non_http_link");
                    }
                    Some(h) => match href_host(h) {
                        Some(hh) if site(hh) == site(page.host) => {}
                        _ => return Effect::Confirm("cross_domain"),
                    },
                }
            }
            Effect::Allow
        }
    }
}

/// Stable code for logs / the Python side: "allow", "confirm:<r>", "deny:<r>".
pub fn code(e: Effect) -> String {
    match e {
        Effect::Allow => "allow".into(),
        Effect::Confirm(r) => format!("confirm:{r}"),
        Effect::Deny(r) => format!("deny:{r}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verbs_and_normalization() {
        assert_eq!(normalize("  Añadir   al CARRITO! "), "anadir al carrito");
        assert!(has_effect_verb("Comprar ahora"));
        assert!(has_effect_verb("Publicar comentario"));
        assert!(has_effect_verb("Place order"));
        assert!(has_effect_verb("Suscríbete"));
        assert!(!has_effect_verb("Repost policy")); // "post" only at a word start
        assert!(!has_effect_verb("Siguiente"));
    }

    #[test]
    fn hosts() {
        assert_eq!(site("WWW.Example.com."), "example.com");
        assert_eq!(
            href_host("https://user@shop.example:8443/x?y"),
            Some("shop.example")
        );
        assert_eq!(href_host("https://[2001:db8::1]/"), Some("2001:db8::1"));
    }
}
