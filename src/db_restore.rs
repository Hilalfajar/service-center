use std::path::PathBuf;
use std::process::Command;

pub const ENGINE_LABELS: [(&str, &str); 2] = [("postgresql", "PostgreSQL"), ("mysql", "MySQL / MariaDB")];
pub const DEFAULT_PORTS: [(&str, &str); 2] = [("postgresql", "5432"), ("mysql", "3306")];

pub fn engine_label(key: &str) -> String {
    match key {
        "postgresql" => "PostgreSQL".into(),
        "mysql" => "MySQL / MariaDB".into(),
        _ => key.to_string(),
    }
}

pub fn default_port(key: &str) -> &'static str {
    match key {
        "postgresql" => "5432",
        "mysql" => "3306",
        _ => "0",
    }
}

#[derive(Debug, Clone)]
pub struct RestoreRequest {
    pub engine: String,
    pub host: String,
    pub port: String,
    pub user: String,
    pub password: String,
    pub database: String,
    pub dump: PathBuf,
    pub create_database: bool,
    pub clean: bool,
}

#[derive(Debug)]
pub struct RestoreError(pub String);

impl std::fmt::Display for RestoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl RestoreRequest {
    pub fn validate(&self) -> Result<(), RestoreError> {
        if !["postgresql", "mysql"].contains(&self.engine.as_str()) {
            return Err(RestoreError("Jenis database belum didukung".into()));
        }
        if self.host.trim().is_empty() {
            return Err(RestoreError("Host wajib diisi".into()));
        }
        if self.user.trim().is_empty() {
            return Err(RestoreError("User wajib diisi".into()));
        }
        if self.host.contains('\n') || self.user.contains('\n') {
            return Err(RestoreError("Host atau user tidak valid".into()));
        }
        if self.host.starts_with('-') || self.user.starts_with('-') {
            return Err(RestoreError("Host atau user tidak boleh diawali tanda minus".into()));
        }
        let port: i64 = self.port.parse().map_err(|_| RestoreError("Port harus berupa angka".into()))?;
        if !(1..=65535).contains(&port) {
            return Err(RestoreError("Port harus di antara 1 dan 65535".into()));
        }
        // db name validation
        let re = regex::Regex::new(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$").unwrap();
        if !re.is_match(&self.database) {
            return Err(RestoreError("Nama database tidak valid".into()));
        }
        Ok(())
    }
}

// Simplified restore: invoke psql/pg_restore or mysql/mariadb with env password.
pub fn restore(req: &RestoreRequest, log: &mut dyn FnMut(String)) -> Result<(), RestoreError> {
    req.validate()?;
    match req.engine.as_str() {
        "postgresql" => restore_postgres(req, log),
        "mysql" => restore_mysql(req, log),
        _ => Err(RestoreError("Jenis database belum didukung".into())),
    }
}

fn restore_postgres(req: &RestoreRequest, log: &mut dyn FnMut(String)) -> Result<(), RestoreError> {
    let ext = req
        .dump
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_lowercase()))
        .unwrap_or_default();
    let is_custom = [".dump", ".backup", ".tar"].contains(&ext.as_str());
    let mut env = std::collections::BTreeMap::new();
    env.insert("PGPASSWORD".to_string(), req.password.clone());

    if req.create_database {
        let _ = Command::new("psql")
            .args(["-h", &req.host, "-p", &req.port, "-U", &req.user, "-d", "postgres", "-c",
                   &format!("CREATE DATABASE \"{}\"", req.database)])
            .envs(&env)
            .output();
    }
    if req.clean {
        let _ = Command::new("psql")
            .args(["-h", &req.host, "-p", &req.port, "-U", &req.user, "-d", &req.database, "-c",
                   "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"])
            .envs(&env)
            .output();
    }

    let status = if is_custom {
        Command::new("pg_restore")
            .args(["-h", &req.host, "-p", &req.port, "-U", &req.user, "-d", &req.database,
                   "-v", req.dump.to_str().unwrap_or("")])
            .envs(&env)
            .status()
    } else {
        Command::new("psql")
            .args(["-h", &req.host, "-p", &req.port, "-U", &req.user, "-d", &req.database,
                   "-f", req.dump.to_str().unwrap_or("")])
            .envs(&env)
            .status()
    };

    match status {
        Ok(s) if s.success() => {
            log("Restore selesai.".into());
            Ok(())
        }
        Ok(s) => Err(RestoreError(format!("Proses gagal (exit {})", s.code().unwrap_or(-1)))),
        Err(e) => Err(RestoreError(format!("Perintah tidak ditemukan: {}", e))),
    }
}

fn restore_mysql(req: &RestoreRequest, log: &mut dyn FnMut(String)) -> Result<(), RestoreError> {
    let mut env = std::collections::BTreeMap::new();
    env.insert("MYSQL_PWD".to_string(), req.password.clone());

    if req.create_database {
        let sql = format!(
            "CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
            req.database
        );
        let _ = Command::new("mysql")
            .args(["-h", &req.host, "-P", &req.port, "-u", &req.user, "-e", &sql])
            .envs(&env)
            .output();
    }
    if req.clean {
        let sql = format!("DROP DATABASE IF EXISTS `{}`; CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;", req.database, req.database);
        let _ = Command::new("mysql")
            .args(["-h", &req.host, "-P", &req.port, "-u", &req.user, "-e", &sql])
            .envs(&env)
            .output();
    }

    // pipe dump file into mysql
    use std::fs::File;
    use std::io::Read;
    let mut f = File::open(&req.dump).map_err(|e| RestoreError(e.to_string()))?;
    let mut data = Vec::new();
    f.read_to_end(&mut data).map_err(|e| RestoreError(e.to_string()))?;

    let mut child = Command::new("mysql")
        .args(["-h", &req.host, "-P", &req.port, "-u", &req.user, &req.database])
        .envs(&env)
        .stdin(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| RestoreError(format!("Perintah tidak ditemukan: {}", e)))?;

    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        let _ = stdin.write_all(&data);
    }
    let status = child.wait().map_err(|e| RestoreError(e.to_string()))?;
    if status.success() {
        log("Restore selesai.".into());
        Ok(())
    } else {
        Err(RestoreError(format!("Proses gagal (exit {})", status.code().unwrap_or(-1))))
    }
}
