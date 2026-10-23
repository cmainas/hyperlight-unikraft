<?php
// Read from stdin and echo each line.
$lines = [];
while (($line = fgets(STDIN)) !== false) {
    $lines[] = rtrim($line, "\n");
}
echo "lines=", count($lines), "\n";
foreach ($lines as $line) {
    echo "echo: $line\n";
}
echo "stdin-done\n";
