<?php
// Concurrency: PHP has no threads, so fibers take turns on one, each one
// suspending where it waits and resuming where it left off.
$log = [];
$workers = [];
foreach (['a', 'b', 'c'] as $name) {
    $workers[$name] = new Fiber(function (int $steps) use ($name, &$log): int {
        $sum = 0;
        for ($i = 1; $i <= $steps; $i++) {
            $log[] = "$name$i";
            $sum += $i * $i;
            Fiber::suspend($i);
        }
        return $sum;
    });
}
foreach ($workers as $fiber) {
    $fiber->start(3);
}
while (array_filter($workers, fn (Fiber $f) => !$f->isTerminated())) {
    foreach ($workers as $fiber) {
        if (!$fiber->isTerminated()) {
            $fiber->resume();
        }
    }
}
echo "order: ", implode(' ', $log), "\n";
echo "sums: ", implode(',', array_map(fn (Fiber $f) => $f->getReturn(), $workers)), "\n";

// A generator pipeline, lazily evaluated.
function squares(int $n): Generator
{
    for ($i = 1; $i <= $n; $i++) {
        yield $i * $i;
    }
}
echo "sum-of-squares: ", array_sum(iterator_to_array(squares(7))), "\n";
echo "fibers-done\n";
