/*
 * hl_reseed.h — reseed the generators a dynamically linked runtime's
 * libraries keep, after the guest is restored from a snapshot.
 *
 * The kernel reseeds its CSPRNG on every restore, so getrandom() and
 * /dev/urandom differ in each guest restored from one snapshot.  A
 * library that seeded a generator of its own from them before the
 * snapshot keeps that state: every restored guest draws the same bytes.
 * hl_reseed_libraries() reseeds the ones a runtime commonly loads, every
 * copy the process has loaded (it loads nothing):
 *
 *   OpenSSL's DRBG (Python's ssl, anything on libcrypto): RAND_seed() with
 *   fresh bytes, which reseeds the primary DRBG, and the public and
 *   private ones with it on their next draw;
 *   SQLite's PRNG (random(), randomblob(), temporary names):
 *   sqlite3_randomness(0, NULL), which reseeds it from the OS.
 *
 * Call it when hl_driver_restored() says so.  A static driver has no
 * loader to ask: it calls these libraries directly, if it links them.
 */

#ifndef HL_RESEED_H
#define HL_RESEED_H

#include <dlfcn.h>
#include <link.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/random.h>

/* The libcrypto and libsqlite3 copies the process has loaded, by prefix,
 * so a wheel's renamed copy (libcrypto-<hash>.so.3) counts, as each copy
 * keeps a generator of its own.  Names only: the loader is not to be
 * entered from dl_iterate_phdr()'s callback. */
#define HL_RESEED_MAX 64

struct hl_reseed_found {
	const char *name[HL_RESEED_MAX];
	int crypto[HL_RESEED_MAX];
	int n;
};

static int hl_reseed_find(struct dl_phdr_info *info, size_t size, void *data)
{
	struct hl_reseed_found *found = data;
	const char *base = strrchr(info->dlpi_name, '/');
	int crypto, sqlite;

	(void)size;
	base = base ? base + 1 : info->dlpi_name;
	crypto = strncmp(base, "libcrypto.so", 12) == 0 ||
		 strncmp(base, "libcrypto-", 10) == 0;
	sqlite = strncmp(base, "libsqlite3.so", 13) == 0 ||
		 strncmp(base, "libsqlite3-", 11) == 0;
	if (!crypto && !sqlite)
		return 0;
	if (found->n == HL_RESEED_MAX) {
		fprintf(stderr, "hl_reseed: more than %d copies of libcrypto and "
			"libsqlite3; %s not reseeded\n", HL_RESEED_MAX, info->dlpi_name);
		return 0;
	}
	found->name[found->n] = info->dlpi_name;
	found->crypto[found->n] = crypto;
	found->n++;
	return 0;
}

static inline void hl_reseed_libraries(void)
{
	struct hl_reseed_found found = { .n = 0 };

	dl_iterate_phdr(hl_reseed_find, &found);
	for (int i = 0; i < found.n; i++) {
		void *h = dlopen(found.name[i], RTLD_LAZY | RTLD_NOLOAD);

		if (!h)
			continue;
		if (found.crypto[i]) {
			void (*rand_seed)(const void *, int) =
				(void (*)(const void *, int))dlsym(h, "RAND_seed");
			unsigned char seed[32];

			if (rand_seed && getrandom(seed, sizeof(seed), 0) == sizeof(seed))
				rand_seed(seed, sizeof(seed));
		} else {
			void (*randomness)(int, void *) =
				(void (*)(int, void *))dlsym(h, "sqlite3_randomness");

			if (randomness)
				randomness(0, NULL);
		}
		dlclose(h);
	}
}

#endif /* HL_RESEED_H */
