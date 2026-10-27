/*
 * munmap() and mprotect() of a range that begins inside a 2 MiB block
 * with no pages mapped yet and ends in the next block reach the pages
 * past the boundary: a mapping made there again reads zeroes, not the
 * memory it had, and a page made read-only faults when written.
 */
#define _GNU_SOURCE
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>

#define MB (1UL << 20)
#define PAGE 4096
#define BEFORE 0x8d000	/* untouched, in the block before the boundary */
#define AFTER 0x70000	/* touched, past the boundary */
#define CHECK(expr) do { \
	if (!(expr)) { \
		printf("page boundary: failed: %s\n", #expr); \
		return 1; \
	} \
} while (0)

static sigjmp_buf env;

static void segv(int sig)
{
	(void)sig;
	siglongjmp(env, 1);
}

/* A range of BEFORE + AFTER bytes whose 2 MiB boundary nothing maps yet */
static char *range(void)
{
	char *big = mmap(NULL, 16 * MB, PROT_NONE,
			 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	uintptr_t boundary;

	if (big == MAP_FAILED)
		return MAP_FAILED;
	boundary = ((uintptr_t)big + 6 * MB) & ~(2 * MB - 1);
	munmap(big, 16 * MB);
	return mmap((char *)boundary - BEFORE, BEFORE + AFTER,
		    PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
}

int main(void)
{
	volatile int faulted = 0;
	size_t i, stale = 0;
	char *p;

	p = range();
	CHECK(p != MAP_FAILED);
	for (i = BEFORE; i < BEFORE + AFTER; i++)
		p[i] = (char)0xa5;
	CHECK(munmap(p, BEFORE + AFTER) == 0);
	CHECK(mmap(p, BEFORE + AFTER, PROT_READ | PROT_WRITE,
		   MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) == p);
	for (i = 0; i < BEFORE + AFTER; i++)
		stale += p[i] != 0;
	CHECK(stale == 0);
	CHECK(munmap(p, BEFORE + AFTER) == 0);

	p = range();
	CHECK(p != MAP_FAILED);
	for (i = BEFORE; i < BEFORE + AFTER; i += PAGE)
		p[i] = 1;
	CHECK(mprotect(p, BEFORE + AFTER, PROT_READ) == 0);
	signal(SIGSEGV, segv);
	if (!sigsetjmp(env, 1))
		p[BEFORE] = 2;
	else
		faulted = 1;
	CHECK(faulted);
	CHECK(munmap(p, BEFORE + AFTER) == 0);
	puts("page boundary: all checks pass");
	return 0;
}
