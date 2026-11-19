use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::process::Command;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CloudflareConfig {
    #[serde(default)]
    pub tunnel: String,
    #[serde(
        rename = "credentials-file",
        default,
        skip_serializing_if = "String::is_empty"
    )]
    pub credentials_file: String,
    #[serde(rename = "no-autoupdate", default)]
    pub no_autoupdate: bool,
    #[serde(default)]
    pub loglevel: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub metrics: String,
    #[serde(default)]
    pub ingress: Vec<IngressRule>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct IngressRule {
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub hostname: String,
    pub service: String,
}

pub fn home() -> PathBuf {
    dirs::home_dir().unwrap_or_default()
}

pub fn default_config_path() -> PathBuf {
    let dir = home().join(".cloudflared");
    let yaml = dir.join("config.yaml");
    let yml = dir.join("config.yml");
    if !yaml.exists() && yml.exists() {
        yml
    } else {
        yaml
    }
}

pub fn find_cloudflared() -> Option<PathBuf> {
    let candidates = [
        home().join(".local/bin/cloudflared"),
        PathBuf::from("/usr/bin/cloudflared"),
        PathBuf::from("/usr/local/bin/cloudflared"),
    ];
    for path in candidates {
        if path.is_file() {
            return Some(path);
        }
    }
    if let Ok(out) = Command::new("which").arg("cloudflared").output() {
        if out.status.success() {
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !s.is_empty() {
                return Some(PathBuf::from(s));
            }
        }
    }
    None
}

pub fn load() -> (CloudflareConfig, PathBuf) {
    let path = default_config_path();
    let config = fs::read_to_string(&path)
        .ok()
        .and_then(|raw| serde_yaml::from_str::<CloudflareConfig>(&raw).ok())
        .unwrap_or_default();
    (config, path)
}

pub fn save(config: &CloudflareConfig, path: &PathBuf) -> Result<(), String> {
    let mut config = config.clone();
    if config.loglevel.trim().is_empty() {
        config.loglevel = "info".into();
    }
    config.ingress.retain(|r| !(r.hostname.trim().is_empty() && r.service.trim().is_empty()));
    let has_catch_all = config
        .ingress
        .last()
        .map(|r| r.hostname.trim().is_empty() && r.service.starts_with("http_status:"))
        .unwrap_or(false);
    if !has_catch_all {
        config.ingress.push(IngressRule {
            hostname: String::new(),
            service: "http_status:404".into(),
        });
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let body = serde_yaml::to_string(&config).map_err(|e| e.to_string())?;
    fs::write(path, body).map_err(|e| e.to_string())
}

/// Parse lines such as:
///   app.example.com = http://localhost:8080
///   app.example.com -> http://localhost:8080
///   http_status:404
pub fn parse_ingress(text: &str) -> Vec<IngressRule> {
    let mut rules = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (host, service) = if let Some((h, s)) = line.split_once("->") {
            (h.trim(), s.trim())
        } else if let Some((h, s)) = line.split_once('=') {
            (h.trim(), s.trim())
        } else {
            ("", line)
        };
        if service.is_empty() {
            continue;
        }
        rules.push(IngressRule {
            hostname: host.to_string(),
            service: service.to_string(),
        });
    }
    rules
}

pub fn ingress_text(rules: &[IngressRule]) -> String {
    rules
        .iter()
        .map(|r| {
            if r.hostname.trim().is_empty() {
                r.service.clone()
            } else {
                format!("{} = {}", r.hostname, r.service)
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn service_command(cloudflared: &PathBuf, config_path: &PathBuf) -> Vec<String> {
    vec![
        cloudflared.to_string_lossy().to_string(),
        "--config".into(),
        config_path.to_string_lossy().to_string(),
        "tunnel".into(),
        "run".into(),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ingress_lines() {
        let rules = parse_ingress(
            "app.example.com = http://localhost:8080\n\nhttp_status:404\n# komentar",
        );
        assert_eq!(rules.len(), 2);
        assert_eq!(rules[0].hostname, "app.example.com");
        assert_eq!(rules[0].service, "http://localhost:8080");
        assert!(rules[1].hostname.is_empty());
        assert_eq!(rules[1].service, "http_status:404");
    }

    #[test]
    fn yaml_skips_empty_optional_fields() {
        let cfg = CloudflareConfig {
            tunnel: "abc".into(),
            credentials_file: String::new(),
            no_autoupdate: true,
            loglevel: "info".into(),
            metrics: String::new(),
            ingress: vec![IngressRule {
                hostname: "app.example.com".into(),
                service: "http://localhost:8080".into(),
            }],
        };
        let text = serde_yaml::to_string(&cfg).unwrap();
        assert!(text.contains("tunnel: abc"));
        assert!(!text.contains("credentials-file"));
        assert!(!text.contains("metrics"));
        assert!(text.contains("hostname: app.example.com"));
    }
}
