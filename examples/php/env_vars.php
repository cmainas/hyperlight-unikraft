<?php
// The host's environment variables: hluk run --env MY_VAR=hello_world ...
foreach (['MY_VAR', 'DEBUG', 'GREETING'] as $key) {
    echo $key, '=', getenv($key) ?: '', "\n";
}
