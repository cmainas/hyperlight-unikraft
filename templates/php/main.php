<?php
// A script: the functions it declares stay for the next call, so a handler
// defined here (or in warm_exec, to have it in the snapshot) can be called
// many times from an embedder with AppSandbox::call("handler", json).
function handler(array $event): array
{
    return ['greeting' => "Hello, {$event['name']}!"];
}

echo "Hello from {{name}}!\n";
echo json_encode(handler(['name' => 'PHP ' . PHP_VERSION . ' on Hyperlight'])), "\n";
