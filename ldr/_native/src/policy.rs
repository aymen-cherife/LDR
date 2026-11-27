use fancy_regex::Regex;
use std::borrow::Cow;

pub struct Policy {
    pub prefixes: Vec<Vec<u8>>,
    pub secrets: Vec<Vec<u8>>,
    pub arguments: Vec<(Vec<u8>, bool)>,
    pub patterns: Vec<Regex>,
    names: regex::Regex,
}
const MARKERS: &[&str] = &[
    "private key",
    "akia",
    "asia",
    "agpa",
    "aida",
    "aroa",
    "anpa",
    "anva",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "_live_",
    "_test_",
    "xox",
    "aiza",
    "sk-",
    "eyj",
    "bearer",
    "://",
    "=",
];
impl Policy {
    pub fn new(
        prefixes: Vec<String>,
        secrets: Vec<Vec<u8>>,
        arguments: Vec<(Vec<u8>, bool)>,
        patterns: Vec<String>,
    ) -> Result<Self, String> {
        let patterns = patterns
            .into_iter()
            .map(|s| Regex::new(&s.replace("\\Z", "\\z")).map_err(|e| e.to_string()))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self{prefixes:prefixes.into_iter().map(String::into_bytes).collect(),secrets,arguments,patterns,
            names:regex::Regex::new(r"(?i)(?:^|_)(?:password|passwd|secret|cl[iİı]ent_?secret|ap[iİı]_?key|ap[iİı]_?token|auth_?token|access_?token|refresh_?token|author[iİı]zat[iİı]on|cook[iİı]e|sess[iİı]on_?[iİı]d|pr[iİı]vate_?key|connect[iİı]on_?str[iİı]ng|credent[iİı]als?)(?:$|_)").unwrap()})
    }
    // None is a policy case delegated to the exact Python detector, never guessed.
    pub fn secret(&self, data: &[u8]) -> Option<bool> {
        for s in &self.secrets {
            if !s.is_empty() && data.windows(s.len()).any(|v| v == s) {
                return Some(true);
            }
        }
        for (s, long) in &self.arguments {
            if !s.is_empty() && (data == s || *long && data.windows(s.len()).any(|v| v == s)) {
                return Some(true);
            }
        }
        let text = String::from_utf8_lossy(data);
        let lower = if data.is_ascii() {
            None
        } else {
            Some(text.to_lowercase())
        };
        if !MARKERS.iter().any(|s| {
            if let Some(ref lower) = lower {
                lower.contains(s)
            } else {
                data.windows(s.len())
                    .any(|v| v.eq_ignore_ascii_case(s.as_bytes()))
            }
        }) {
            return Some(false);
        }
        // Rust and CPython regex engines have different Unicode boundary tables.
        // Only ASCII shape matching is claimed native; all other cases delegate.
        if !data.is_ascii() {
            return None;
        }
        for p in &self.patterns {
            match p.is_match(&text) {
                Ok(true) => return Some(true),
                Ok(false) => {}
                Err(_) => return None,
            }
        }
        Some(false)
    }
    pub fn secret_name(&self, data: &[u8]) -> bool {
        let text = String::from_utf8_lossy(data);
        if !text.as_bytes().windows(2).any(|w| {
            (w[0].is_ascii_lowercase() || w[0].is_ascii_digit()) && w[1].is_ascii_uppercase()
        }) {
            return self.names.is_match(&text);
        }
        let mut camel = String::new();
        let mut previous = None;
        for c in text.chars() {
            if c.is_ascii_uppercase()
                && previous.is_some_and(|p: char| p.is_ascii_lowercase() || p.is_ascii_digit())
            {
                camel.push('_')
            }
            camel.push(c);
            previous = Some(c);
        }
        self.names.is_match(&camel)
    }
    pub fn normalize<'a>(&self, bytes: &'a [u8]) -> (Cow<'a, [u8]>, usize) {
        if !self
            .prefixes
            .iter()
            .any(|p| !p.is_empty() && bytes.windows(p.len()).any(|s| s == p))
        {
            return (Cow::Borrowed(bytes), 0);
        }
        let mut result = Vec::new();
        let mut cursor = 0;
        let mut copied = 0;
        let mut count = 0;
        while cursor < bytes.len() {
            let mut end = None;
            for prefix in &self.prefixes {
                if prefix.is_empty() || !bytes[cursor..].starts_with(prefix) {
                    continue;
                }
                let mut next = cursor + prefix.len();
                if next < bytes.len() && (bytes[next] == b'/' || bytes[next] == b'\\') {
                    while next < bytes.len() && !boundary(bytes, next) {
                        next += width(bytes[next]);
                    }
                }
                if next == bytes.len() || boundary(bytes, next) {
                    end = Some(next);
                    break;
                }
            }
            if let Some(next) = end {
                result.extend_from_slice(&bytes[copied..cursor]);
                result.extend_from_slice("<tmp>/…".as_bytes());
                copied = next;
                cursor = next;
                count += 1;
            } else {
                cursor += width(bytes[cursor]);
            }
        }
        if count == 0 {
            (Cow::Borrowed(bytes), 0)
        } else {
            result.extend_from_slice(&bytes[copied..]);
            (Cow::Owned(result), count)
        }
    }
}
fn width(b: u8) -> usize {
    if b < 128 {
        1
    } else if b < 224 {
        2
    } else if b < 240 {
        3
    } else {
        4
    }
}
fn boundary(s: &[u8], i: usize) -> bool {
    let b = s[i];
    if b == b'\'' || b == b'"' {
        return true;
    }
    let w = width(b);
    let c = match w {
        1 => b as u32,
        2 => ((b & 31) as u32) << 6 | (s[i + 1] & 63) as u32,
        3 => ((b & 15) as u32) << 12 | ((s[i + 1] & 63) as u32) << 6 | (s[i + 2] & 63) as u32,
        _ => 0,
    };
    matches!(c,9..=13|28..=32|0x85|0xa0|0x1680|0x2000..=0x200a|0x2028|0x2029|0x202f|0x205f|0x3000)
}
