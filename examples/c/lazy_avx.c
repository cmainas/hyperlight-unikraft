/*
 * The kernel leaves AVX off until a program uses it: CPUID reports no
 * OSXSAVE at first, an AVX instruction then runs instead of raising
 * SIGILL, and CPUID reports OSXSAVE after.  A thread blocked in a system
 * call while that happens gets its SSE registers back as they were, and
 * a thread's AVX registers, upper halves included, survive another
 * thread using them.  On a CPU with AVX-512, an AVX-512 instruction then
 * enables that too, and zmm16 survives the same way.
 *
 * With "restored", run in a sandbox restored from a snapshot taken after
 * a first run: AVX is on from the start, and still works.
 */
#include <stdio.h>

#if defined(__x86_64__)
#include <cpuid.h>
#include <pthread.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#define CHECK(expr) do { \
	if (!(expr)) { \
		printf("lazy avx: failed: %s\n", #expr); \
		return 1; \
	} \
} while (0)

#define CPUID1_ECX_OSXSAVE (1u << 27)
#define CPUID1_ECX_AVX (1u << 28)
#define CPUID7_EBX_AVX512F (1u << 16)
#define XCR0_AVX512 (7u << 5)

static int pipefd[2];
static unsigned char sse_pattern[16], sse_after[16];

static unsigned int cpuid1_ecx(void)
{
	unsigned int eax, ebx, ecx, edx;

	__cpuid(1, eax, ebx, ecx, edx);
	return ecx;
}

/*
 * Holds a pattern in xmm8 across a read() that blocks until the main
 * thread has used AVX.  One asm block, so nothing else touches xmm8.
 */
static void *blocked_reader(void *arg)
{
	char c;
	long ret;

	(void)arg;
	__asm__ volatile(
		"movdqu (%[in]), %%xmm8\n\t"
		"syscall\n\t"
		"movdqu %%xmm8, (%[out])\n\t"
		: "=a"(ret)
		: "0"((long)SYS_read), "D"((long)pipefd[0]), "S"(&c),
		  "d"(1L), [in] "r"(sse_pattern), [out] "r"(sse_after)
		: "rcx", "r11", "xmm8", "memory");
	return (void *)ret;
}

static int ymm_pipe[2];

/* Overwrites ymm9 and wakes the main thread, in one asm block */
static void *ymm_clobberer(void *arg)
{
	static const unsigned char other[32] = { [0 ... 31] = 0xee };
	long ret = SYS_write;
	char c = 'y';

	(void)arg;
	__asm__ volatile(
		"vmovdqu (%[in]), %%ymm9\n\t"
		"syscall\n\t"
		: "+a"(ret)
		: "D"((long)ymm_pipe[1]), "S"(&c), "d"(1L),
		  [in] "r"(other)
		: "rcx", "r11", "xmm9", "memory");
	return (void *)ret;
}

/*
 * ymm9, upper half included, survives a read() that blocks while another
 * thread sets ymm9 to something else: the context store saves AVX state.
 */
static int check_ymm_across_switch(void)
{
	unsigned char ymm_pattern[32], ymm_after[32];
	pthread_t clobberer;
	long ret = SYS_read;
	void *wret;
	char c;
	int i;

	for (i = 0; i < 32; i++)
		ymm_pattern[i] = 0x10 + i;
	CHECK(pipe(ymm_pipe) == 0);
	CHECK(pthread_create(&clobberer, NULL, ymm_clobberer, NULL) == 0);
	__asm__ volatile(
		"vmovdqu (%[in]), %%ymm9\n\t"
		"syscall\n\t"
		"vmovdqu %%ymm9, (%[out])\n\t"
		: "+a"(ret)
		: "D"((long)ymm_pipe[0]), "S"(&c), "d"(1L),
		  [in] "r"(ymm_pattern), [out] "r"(ymm_after)
		: "rcx", "r11", "xmm9", "memory");
	CHECK(ret == 1);
	CHECK(pthread_join(clobberer, &wret) == 0);
	CHECK((long)wret == 1);
	CHECK(memcmp(ymm_pattern, ymm_after, sizeof(ymm_after)) == 0);
	return 0;
}

static int zmm_pipe[2];

/* Overwrites zmm16 and wakes the main thread, in one asm block */
static void *zmm_clobberer(void *arg)
{
	static const unsigned char other[64] = { [0 ... 63] = 0xee };
	long ret = SYS_write;
	char c = 'z';

	(void)arg;
	__asm__ volatile(
		"vmovdqu64 (%[in]), %%zmm16\n\t"
		"syscall\n\t"
		: "+a"(ret)
		: "D"((long)zmm_pipe[1]), "S"(&c), "d"(1L),
		  [in] "r"(other)
		: "rcx", "r11", "memory");
	return (void *)ret;
}

/*
 * On a CPU with AVX-512, the first AVX-512 instruction enables it, and
 * zmm16 survives a read() that blocks while another thread sets it.
 * Built without -mavx512f, the compiler never uses zmm16 (nor lets an
 * asm clobber it).
 */
static int check_zmm_across_switch(void)
{
	unsigned char zmm_pattern[64], zmm_after[64];
	unsigned int eax, ebx, ecx, edx;
	pthread_t clobberer;
	long ret = SYS_read;
	void *wret;
	char c;
	int i;

	__cpuid_count(7, 0, eax, ebx, ecx, edx);
	if (!(ebx & CPUID7_EBX_AVX512F))
		return 0;
	__cpuid_count(0xd, 0, eax, ebx, ecx, edx);
	if ((eax & XCR0_AVX512) != XCR0_AVX512)
		return 0;
	/* A VEX opmask instruction, the first AVX-512 one compilers emit */
	__asm__ volatile("kxnorw %%k1, %%k1, %%k1" :::);

	for (i = 0; i < 64; i++)
		zmm_pattern[i] = 0x40 + i;
	CHECK(pipe(zmm_pipe) == 0);
	CHECK(pthread_create(&clobberer, NULL, zmm_clobberer, NULL) == 0);
	__asm__ volatile(
		"vmovdqu64 (%[in]), %%zmm16\n\t"
		"syscall\n\t"
		"vmovdqu64 %%zmm16, (%[out])\n\t"
		: "+a"(ret)
		: "D"((long)zmm_pipe[0]), "S"(&c), "d"(1L),
		  [in] "r"(zmm_pattern), [out] "r"(zmm_after)
		: "rcx", "r11", "memory");
	CHECK(ret == 1);
	CHECK(pthread_join(clobberer, &wret) == 0);
	CHECK((long)wret == 1);
	CHECK(memcmp(zmm_pattern, zmm_after, sizeof(zmm_after)) == 0);
	return 0;
}

int main(int argc, char **argv)
{
	pthread_t reader;
	void *ret;
	int i;

	if (!(cpuid1_ecx() & CPUID1_ECX_AVX)) {
		printf("lazy avx: no AVX on this CPU, skipped\n");
		return 0;
	}
	if (argc > 1 && strcmp(argv[1], "restored") == 0) {
		CHECK(cpuid1_ecx() & CPUID1_ECX_OSXSAVE);
		__asm__ volatile("vpxor %%ymm0, %%ymm0, %%ymm0" ::: "xmm0");
		if (check_ymm_across_switch() || check_zmm_across_switch())
			return 1;
		printf("lazy avx: restored, all checks pass\n");
		return 0;
	}
	CHECK(!(cpuid1_ecx() & CPUID1_ECX_OSXSAVE));

	for (i = 0; i < 16; i++)
		sse_pattern[i] = 0xa0 + i;
	CHECK(pipe(pipefd) == 0);
	CHECK(pthread_create(&reader, NULL, blocked_reader, NULL) == 0);
	/*
	 * Let the reader block in read() with its context saved: threads
	 * are not preempted, so once it runs it runs until read() blocks.
	 */
	usleep(100 * 1000);

	/* The first AVX instruction: enables AVX and runs */
	__asm__ volatile("vpxor %%ymm0, %%ymm0, %%ymm0" ::: "xmm0");
	CHECK(cpuid1_ecx() & CPUID1_ECX_OSXSAVE);

	if (check_ymm_across_switch() || check_zmm_across_switch())
		return 1;

	CHECK(write(pipefd[1], "x", 1) == 1);
	CHECK(pthread_join(reader, &ret) == 0);
	CHECK((long)ret == 1);
	CHECK(memcmp(sse_pattern, sse_after, sizeof(sse_after)) == 0);

	printf("lazy avx: all checks pass\n");
	return 0;
}
#else
int main(void)
{
	printf("lazy avx: not x86_64, skipped\n");
	return 0;
}
#endif /* __x86_64__ */
