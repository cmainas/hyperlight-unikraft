<?php
// A guest function: load it once, then call it as often as you like.
//
//     hluk run --initrd build-elfloader/php-rootfs.cpio examples/php/handler.php \
//         --call handler --input '{"name":"World"}'
//
// or from Rust: sandbox.run(Exec::File("examples/php/handler.php".into()))?, then
// sandbox.call("handler", r#"{"name":"World"}"#)?  // {"greeting":"Hello, World!","calls":1}
//
// The input arrives decoded, JSON objects as arrays; the array returned goes
// back as a JSON object. `handler` and `$calls` are globals the next call sees.
$calls = 0;

function handler(array $event): array
{
    global $calls;
    $calls++;
    return ['greeting' => "Hello, {$event['name']}!", 'calls' => $calls];
}
