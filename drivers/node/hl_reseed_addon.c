/*
 * hl_reseed.node — Node's generators, fresh after a restore from a
 * snapshot.
 *
 * Node links OpenSSL in and exports its symbols for addons; the DRBG it
 * keeps (crypto.randomBytes(), TLS) was seeded before the snapshot, and
 * Node offers no way to reseed it.  RAND_seed() with fresh bytes from the
 * kernel, reseeded on restore, does: the child's bootstrap calls
 * reseed() when the driver says the guest was restored (hl_child.h).
 *
 * V8's Math.random cannot be reseeded either, so the bootstrap replaces
 * it with draws from the kernel's generator, which fill() hands it 64 at
 * a time: about 100 ns a draw in the guest, where a generator written in
 * JS measured 700 ns (V8's own, 26 ns).
 */

#include <node_api.h>
#include <stdint.h>
#include <string.h>
#include <sys/random.h>

/* Node's OpenSSL, resolved in the node binary when the addon loads. */
void RAND_seed(const void *buf, int num);

static napi_value reseed(napi_env env, napi_callback_info info)
{
	unsigned char seed[32];

	(void)env;
	(void)info;
	if (getrandom(seed, sizeof(seed), 0) == sizeof(seed))
		RAND_seed(seed, sizeof(seed));
	return NULL;
}

/* fill(Float64Array): doubles in [0, 1), 53 random bits each, as V8's. */
static napi_value fill(napi_env env, napi_callback_info info)
{
	size_t argc = 1, len, n, done = 0;
	napi_value arg;
	napi_typedarray_type type;
	void *data;

	if (napi_get_cb_info(env, info, &argc, &arg, NULL, NULL) != napi_ok || argc < 1 ||
	    napi_get_typedarray_info(env, arg, &type, &len, &data, NULL, NULL) != napi_ok ||
	    type != napi_float64_array) {
		napi_throw_type_error(env, NULL, "fill() takes a Float64Array");
		return NULL;
	}
	n = len * sizeof(double);
	while (done < n) {
		ssize_t r = getrandom((char *)data + done, n - done, 0);

		if (r <= 0) {
			napi_throw_error(env, NULL, "getrandom failed");
			return NULL;
		}
		done += (size_t)r;
	}
	for (size_t i = 0; i < len; i++) {
		uint64_t x;

		memcpy(&x, (double *)data + i, sizeof(x));
		((double *)data)[i] = (double)(x >> 11) * 0x1p-53;
	}
	return NULL;
}

NAPI_MODULE_INIT()
{
	napi_value fn;

	if (napi_create_function(env, "reseed", NAPI_AUTO_LENGTH, reseed, NULL, &fn) != napi_ok ||
	    napi_set_named_property(env, exports, "reseed", fn) != napi_ok ||
	    napi_create_function(env, "fill", NAPI_AUTO_LENGTH, fill, NULL, &fn) != napi_ok ||
	    napi_set_named_property(env, exports, "fill", fn) != napi_ok)
		return NULL;
	return exports;
}
