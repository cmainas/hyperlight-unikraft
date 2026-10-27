/*
 * What a process maps is unmapped when it ends: children that each map
 * more than the guest has run one after another, the mappings a child
 * leaves behind go with it, whether it exits, is killed or execs from a
 * thread, and a failed exec leaves the caller's memory as it was.  Memory
 * shared with CLONE_VM outlives the child that mapped it.  A thread that
 * execs a program without libc (no_libc.c) leaves no reference into the
 * memory it unmapped for the kernel to write at exit.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/auxv.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#define PAGE 4096
#define CHILD_MB 16
#define CHILDREN 24
#define CHECK(expr) do { \
	if (!(expr)) { \
		printf("process memory: failed: %s (errno %d)\n", #expr, \
		       errno); \
		return 1; \
	} \
} while (0)

static char **args;

/* Mappings of the guest, one per line of /dev/maps */
static int mappings(void)
{
	FILE *maps = fopen("/dev/maps", "r");
	char line[512];
	int count = 0;

	if (!maps)
		return -1;
	while (fgets(line, sizeof(line), maps))
		count++;
	fclose(maps);
	return count;
}

/* Map and touch CHILD_MB, split the mapping, and leave it all mapped.
 * AT_EXECFN still names this program after: its path is a copy, not the
 * exec caller's string, which may have gone with the caller's memory.
 */
static int child_maps(void)
{
	size_t len = (size_t)CHILD_MB << 20;
	char *memory = mmap(NULL, len, PROT_READ | PROT_WRITE,
			    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	const char *execfn;

	CHECK(memory != MAP_FAILED);
	memset(memory, 42, len);
	CHECK(mprotect(memory + PAGE, PAGE, PROT_READ) == 0);
	CHECK(munmap(memory + 4 * PAGE, 2 * PAGE) == 0);
	execfn = (const char *)getauxval(AT_EXECFN);
	CHECK(execfn && !strcmp(execfn, args[0]));
	return 0;
}

static void *thread_exits(void *arg)
{
	(void)arg;
	exit(child_maps() ? 1 : 7);
}

/* With the path in memory of this process's own, which the exec unmaps:
 * large enough for malloc() to map it
 */
static void *thread_execs(void *arg)
{
	char *path = malloc(256 * 1024);
	char *argv[] = { path, "maps", NULL };

	(void)arg;
	if (!path)
		_exit(126);
	strcpy(path, args[0]);
	execv(path, argv);
	_exit(127);
}

/* no_libc, next to this program */
static void *thread_execs_no_libc(void *arg)
{
	char path[PATH_MAX], *slash;
	char *argv[] = { path, NULL };

	(void)arg;
	snprintf(path, sizeof(path), "%s", args[0]);
	slash = strrchr(path, '/');
	snprintf(slash ? slash + 1 : path,
		 sizeof(path) - (slash ? slash + 1 - path : 0), "no_libc");
	execv(path, argv);
	_exit(127);
}

/* Run this program again as `mode`, and return how it ended */
static int run(const char *mode, int *status)
{
	char *argv[] = { args[0], (char *)mode, NULL };
	pid_t pid = vfork();

	if (pid < 0)
		return -1;
	if (!pid) {
		execv(args[0], argv);
		_exit(127);
	}
	return waitpid(pid, status, 0) == pid ? 0 : -1;
}

static int shared_child(void *arg)
{
	char **memory = arg;

	*memory = mmap(NULL, PAGE, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (*memory == MAP_FAILED)
		return 1;
	(*memory)[0] = 73;
	return 0;
}

int main(int argc, char **argv)
{
	char *parent, *stack, *shared = NULL;
	pthread_t thread;
	int before, status, i;

	args = argv;
	if (argc == 2 && !strcmp(argv[1], "maps"))
		return child_maps();
	if (argc == 2 && (!strcmp(argv[1], "exit") ||
			  !strcmp(argv[1], "exec") ||
			  !strcmp(argv[1], "no_libc"))) {
		CHECK(child_maps() == 0);
		CHECK(pthread_create(&thread, NULL,
				     !strcmp(argv[1], "exit") ? thread_exits :
				     !strcmp(argv[1], "exec") ? thread_execs :
				     thread_execs_no_libc,
				     NULL) == 0);
		for (;;)
			pause();
	}
	if (argc == 2 && !strcmp(argv[1], "kill")) {
		CHECK(child_maps() == 0);
		raise(SIGTERM);
		return 1;
	}

	parent = mmap(NULL, PAGE, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(parent != MAP_FAILED);
	parent[0] = 19;
	before = mappings();
	CHECK(before > 0);

	/* More than the guest has, in children that exit */
	for (i = 0; i < CHILDREN; i++) {
		CHECK(run("maps", &status) == 0);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}

	/* Children killed by a signal, and children that end from a thread,
	 * by exit() or by execv() of no_libc or of this program
	 */
	for (i = 0; i < CHILDREN / 3; i++) {
		CHECK(run("kill", &status) == 0);
		CHECK(WIFSIGNALED(status) && WTERMSIG(status) == SIGTERM);
		CHECK(run("exit", &status) == 0);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 7);
		CHECK(run("no_libc", &status) == 0);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 5);
		CHECK(run("exec", &status) == 0);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}

	/* A failed exec, from a vfork() child on the parent's memory */
	{
		char *missing[] = { "/missing-executable", NULL };
		pid_t pid = vfork();

		CHECK(pid >= 0);
		if (!pid) {
			execv(missing[0], missing);
			_exit(errno == ENOENT ? 0 : 1);
		}
		CHECK(waitpid(pid, &status, 0) == pid);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
	CHECK(parent[0] == 19);

	/* A child that shares the memory: what it maps stays */
	stack = mmap(NULL, 64 * PAGE, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(stack != MAP_FAILED);
	CHECK(clone(shared_child, stack + 64 * PAGE, CLONE_VM | SIGCHLD,
		    &shared) > 0);
	CHECK(wait(&status) > 0);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	CHECK(shared && shared != MAP_FAILED && shared[0] == 73);
	CHECK(munmap(shared, PAGE) == 0);
	CHECK(munmap(stack, 64 * PAGE) == 0);

	/* Kernel thread stacks are freed by the idle thread, when it runs:
	 * give it up to 100 ms
	 */
	for (i = 0; i < 100 && mappings() > before + 4; i++)
		CHECK(usleep(1000) == 0);
	CHECK(mappings() <= before + 4);
	CHECK(parent[0] == 19);
	CHECK(munmap(parent, PAGE) == 0);
	printf("process memory: all checks pass, %d children of %d MiB\n",
	       CHILDREN + 4 * (CHILDREN / 3), CHILD_MB);
	return 0;
}
