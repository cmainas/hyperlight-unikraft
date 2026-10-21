// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! The driver in a ClickHouse server of its own: `CREATE FUNCTION ...
//! ENGINE = HyperlightPython`, then queries.  Needs what the serve tests
//! need, and CLICKHOUSE_BINARY, the `clickhouse` binary of a release with
//! driver-based UDFs (26.6 or later).

mod common;

use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use common::{BIN, initrd};

struct Server {
    child: Child,
    clickhouse: PathBuf,
    port: u16,
    _dir: tempfile::TempDir,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.child.kill().ok();
        self.child.wait().ok();
    }
}

impl Server {
    fn start(clickhouse: PathBuf, initrd: &Path) -> Server {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        // A port nothing listens on now
        let port = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let drivers = root.join("drivers");
        std::fs::create_dir(&drivers).unwrap();
        let create = drivers.join("hyperlight_python_create.sh");
        std::fs::write(&create, format!("#!/bin/sh\nexec '{BIN}' create \"$@\"\n")).unwrap();
        std::fs::set_permissions(&create, PermissionsExt::from_mode(0o755)).unwrap();
        std::fs::write(
            drivers.join("hyperlight_python_driver.xml"),
            format!(
                "<clickhouse><driver><name>HyperlightPython</name>\
                 <create_command>hyperlight_python_create.sh</create_command>\
                 <env><CLICKHOUSE_HYPERLIGHT_INITRD>{}</CLICKHOUSE_HYPERLIGHT_INITRD>\
                 <CLICKHOUSE_HYPERLIGHT_POOL_SIZE>4</CLICKHOUSE_HYPERLIGHT_POOL_SIZE></env>\
                 </driver></clickhouse>",
                initrd.display()
            ),
        )
        .unwrap();
        let path = |name: &str| format!("{}/{name}/", root.display());
        std::fs::write(
            root.join("config.xml"),
            format!(
                "<clickhouse>
                    <logger><level>warning</level><console>1</console></logger>
                    <tcp_port>{port}</tcp_port>
                    <listen_host>127.0.0.1</listen_host>
                    <path>{data}</path>
                    <tmp_path>{data}tmp/</tmp_path>
                    <user_scripts_path>{scripts}</user_scripts_path>
                    <allow_experimental_executable_udf_drivers>true</allow_experimental_executable_udf_drivers>
                    <user_defined_executable_function_drivers_config>drivers/*_driver.xml</user_defined_executable_function_drivers_config>
                    <dynamic_user_defined_executable_functions_path>{dynamic}</dynamic_user_defined_executable_functions_path>
                    <users><default><password></password><networks><ip>127.0.0.1</ip></networks>
                        <profile>default</profile><quota>default</quota></default></users>
                    <profiles><default/></profiles>
                    <quotas><default/></quotas>
                </clickhouse>",
                data = path("data"),
                scripts = path("scripts"),
                dynamic = path("dynamic"),
            ),
        )
        .unwrap();
        let child = Command::new(&clickhouse)
            .arg("server")
            .arg(format!(
                "--config-file={}",
                root.join("config.xml").display()
            ))
            .current_dir(root)
            .stdout(Stdio::null())
            // Its console log, which says why it did not start, if it does not
            .stderr(std::fs::File::create(root.join("server.log")).unwrap())
            .spawn()
            .unwrap();
        let log = root.join("server.log");
        let server = Server {
            child,
            clickhouse,
            port,
            _dir: dir,
        };
        let deadline = Instant::now() + Duration::from_secs(60);
        while server.query("SELECT 1").is_err() {
            assert!(
                Instant::now() < deadline,
                "the ClickHouse server did not start:\n{}",
                std::fs::read_to_string(&log).unwrap_or_default()
            );
            std::thread::sleep(Duration::from_millis(250));
        }
        server
    }

    /// Run `sql`: its output, or the server's error.
    fn query(&self, sql: &str) -> Result<String, String> {
        let output = Command::new(&self.clickhouse)
            .args(["client", "--port", &self.port.to_string(), "--query", sql])
            .output()
            .unwrap();
        if output.status.success() {
            Ok(String::from_utf8(output.stdout)
                .unwrap()
                .trim_end()
                .to_string())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).into_owned())
        }
    }
}

#[test]
fn creates_and_runs_functions() {
    let Some(initrd) = initrd() else { return };
    let Some(clickhouse) = std::env::var_os("CLICKHOUSE_BINARY").map(PathBuf::from) else {
        eprintln!("skipped: set CLICKHOUSE_BINARY to a clickhouse binary");
        return;
    };
    let server = Server::start(clickhouse, &initrd);

    server
        .query(
            "CREATE FUNCTION py_describe ARGUMENTS (b Bool, i Int64, f Float64, s Nullable(String), a Array(UInt8)) \
             RETURNS String ENGINE = HyperlightPython AS 'return repr((b, i, f, s, a))'",
        )
        .unwrap();
    assert_eq!(
        server
            .query("SELECT py_describe(true, -9223372036854775808, nan, NULL, [1, 2])")
            .unwrap(),
        r"(True, -9223372036854775808, nan, None, [1, 2])"
    );

    server
        .query(
            "CREATE FUNCTION py_words ARGUMENTS (s String) RETURNS UInt64 ENGINE = HyperlightPython \
             AS 'return len(s.split())'",
        )
        .unwrap();
    // Some blocks, on as many guests as the pool holds
    assert_eq!(
        server
            .query(
                "SELECT sum(py_words(concat('a b ', toString(number)))) FROM numbers_mt(1000000)"
            )
            .unwrap(),
        "3000000"
    );

    server
        .query("CREATE FUNCTION py_fails ARGUMENTS (x UInt8) RETURNS UInt8 ENGINE = HyperlightPython AS 'return 1 / x'")
        .unwrap();
    let error = server.query("SELECT py_fails(0)").unwrap_err();
    assert!(
        error.contains("ZeroDivisionError") && error.contains("py_fails failed on row 1"),
        "{error}"
    );

    let error = server
        .query("CREATE FUNCTION py_bad ARGUMENTS (x UInt8) RETURNS UInt8 ENGINE = HyperlightPython AS 'return x +'")
        .unwrap_err();
    assert!(error.contains("SyntaxError"), "{error}");

    server.query("DROP FUNCTION py_words").unwrap();
    assert!(server.query("SELECT py_words('a')").is_err());
}
