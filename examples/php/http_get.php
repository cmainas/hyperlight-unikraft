<?php
// HTTP GET over host networking.  Run with: --net
$url = 'http://httpbin.org/get';
echo "GET $url\n";
$context = stream_context_create(['http' => ['timeout' => 10, 'ignore_errors' => true]]);
$body = file_get_contents($url, false, $context);
if ($body === false) {
    fwrite(STDERR, "Request failed\n");
    exit(1);
}
// $http_response_header is deprecated in PHP 8.5: the headers come from the stream.
[$status] = http_get_last_response_headers();
echo 'Status: ', explode(' ', $status)[1], "\n";
echo substr($body, 0, 500), "\n";
