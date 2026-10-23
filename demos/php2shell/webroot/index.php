<?php
// The legitimate application: it greets the caller.  The whole point of
// the sandbox is that this keeps working while the attacker's payload
// (payload.php, run as if the engine were popped) gets nowhere.
$who = $argv[1] ?? 'world';
echo "Hello, $who, from PHP on Hyperlight!\n";
