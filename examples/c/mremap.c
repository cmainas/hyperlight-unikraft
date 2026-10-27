/*
 * mremap() resizes a mapping in place with the errors Linux gives, and
 * never moves one: MREMAP_MAYMOVE resizes in place too, and fails with
 * ENOMEM where only a move would do.  musl finds the main thread's stack
 * with these calls, so pthread_getattr_np() reports the whole stack, not
 * the single page it reported while mremap() failed with ENOSYS.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>

#define PAGE 4096
#define CHECK(expr) do { \
	if (!(expr)) { \
		printf("mremap: failed: %s (errno %d)\n", #expr, errno); \
		return 1; \
	} \
} while (0)

int main(void)
{
	pthread_attr_t attr;
	void *stack;
	size_t stack_size;
	char *base;

	CHECK(pthread_getattr_np(pthread_self(), &attr) == 0);
	CHECK(pthread_attr_getstack(&attr, &stack, &stack_size) == 0);
	pthread_attr_destroy(&attr);
	CHECK(stack_size >= 1024 * 1024);

	base = mmap(NULL, 8 * PAGE, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(base != MAP_FAILED);
	base[0] = 42;
	base[PAGE] = 43;
	CHECK(munmap(base + 2 * PAGE, 6 * PAGE) == 0);

	/* Errors, none of which may change the mapping.  The last is a move,
	 * which Linux would make and this kernel does not.
	 */
	CHECK(mremap(base, PAGE, 2 * PAGE, 0) == MAP_FAILED && errno == ENOMEM);
	CHECK(mremap(base + 4 * PAGE, PAGE, 2 * PAGE, 0) == MAP_FAILED &&
	      errno == EFAULT);
	CHECK(mremap(base, 3 * PAGE, 4 * PAGE, 0) == MAP_FAILED &&
	      errno == EFAULT);
	CHECK(mremap(base + 1, PAGE, PAGE, 0) == MAP_FAILED && errno == EINVAL);
	CHECK(mremap(base, PAGE, 0, 0) == MAP_FAILED && errno == EINVAL);
	/* musl refuses this one itself, as on Linux */
	CHECK(mremap(base, PAGE, SIZE_MAX, 0) == MAP_FAILED && errno == ENOMEM);
	CHECK(mremap(base, PAGE, PAGE, MREMAP_FIXED, base) == MAP_FAILED &&
	      errno == EINVAL);
	CHECK(mremap(base, PAGE, PAGE, MREMAP_MAYMOVE | MREMAP_FIXED,
		     base + 4 * PAGE) == MAP_FAILED && errno == ENOMEM);
	CHECK(mremap(base, 2 * PAGE, PAGE, MREMAP_MAYMOVE | MREMAP_FIXED,
		     base + PAGE) == MAP_FAILED && errno == EINVAL);
	CHECK(base[0] == 42 && base[PAGE] == 43);

	/* Growth and shrinking in place, with and without MREMAP_MAYMOVE */
	CHECK(mremap(base, 2 * PAGE, 3 * PAGE, 0) == base);
	CHECK(base[0] == 42 && base[PAGE] == 43 && base[2 * PAGE] == 0);
	CHECK(mremap(base, 3 * PAGE, 2 * PAGE, MREMAP_MAYMOVE) == base);
	CHECK(mremap(base, 2 * PAGE, 3 * PAGE, MREMAP_MAYMOVE) == base);
	CHECK(base[0] == 42 && base[PAGE] == 43 && base[2 * PAGE] == 0);
	base[2 * PAGE] = 44;

	/* A mapping in the way: growth fails, MREMAP_MAYMOVE or not */
	CHECK(mmap(base + 3 * PAGE, PAGE, PROT_NONE,
		   MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE,
		   -1, 0) == base + 3 * PAGE);
	CHECK(mremap(base, 3 * PAGE, 4 * PAGE, 0) == MAP_FAILED &&
	      errno == ENOMEM);
	CHECK(mremap(base, 3 * PAGE, 4 * PAGE, MREMAP_MAYMOVE) == MAP_FAILED &&
	      errno == ENOMEM);
	CHECK(base[2 * PAGE] == 44);

	/* Shrinking unmaps the tail, though it spans two mappings */
	CHECK(mprotect(base + PAGE, PAGE, PROT_READ) == 0);
	CHECK(mremap(base, 3 * PAGE, 3 * PAGE, 0) == base);
	CHECK(mremap(base, 3 * PAGE, PAGE, 0) == base);
	CHECK(base[0] == 42);
	CHECK(mremap(base + PAGE, PAGE, PAGE, 0) == MAP_FAILED &&
	      errno == EFAULT);
	CHECK(munmap(base, 4 * PAGE) == 0);
	puts("mremap: all checks pass");
	return 0;
}
