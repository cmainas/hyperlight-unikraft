<?php
// Filesystem I/O against a host-mounted directory.
// Run with: --mount <hostdir>:/mnt/host
$path = '/mnt/host/php_fs.txt';
file_put_contents($path, 'hello from php');
echo "read-back: ", file_get_contents($path), "\n";

$lines = '/mnt/host/php_lines.txt';
file_put_contents($lines, implode("\n", ['a', 'b', 'c']) . "\n");
echo "line-count: ", count(file($lines)), "\n";

mkdir('/mnt/host/php_dir');
touch('/mnt/host/php_dir/x');
echo "listing: ", implode(',', array_diff(scandir('/mnt/host/php_dir'), ['.', '..'])), "\n";
unlink('/mnt/host/php_dir/x');
rmdir('/mnt/host/php_dir');
unlink($lines);

echo "exists: ", var_export(file_exists($path), true), "\n";
echo "fs-ops-done\n";
