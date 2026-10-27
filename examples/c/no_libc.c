/*
 * A program without libc, which exits with status 5 at once.  It never
 * calls set_tid_address(), as a Go binary's main thread does not: the
 * kernel must not write a child TID reference left from the image that
 * exec'd it (process_memory.c).  Built with -nostdlib.
 */
void _start(void)
{
#if defined(__x86_64__)
	__asm__ volatile("syscall" : : "a"(231), "D"(5)); /* exit_group(5) */
#elif defined(__aarch64__)
	register long x0 __asm__("x0") = 5;
	register long x8 __asm__("x8") = 94; /* exit_group */

	__asm__ volatile("svc #0" : : "r"(x0), "r"(x8));
#else
#error "no_libc.c: unsupported architecture"
#endif
	for (;;)
		;
}
