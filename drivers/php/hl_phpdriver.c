/*
 * hl_phpdriver — PHP runtime driver for Hyperlight.
 *
 * Embeds PHP (the embed SAPI): the engine and one request are brought up
 * once and kept for the guest's life, so what a call defines (a function,
 * a class, a global, an included file) is there for the next, and a warm
 * snapshot has the engine ready.  `php -a` keeps its state the same way.
 *
 *   Exec       the call's source.  Source that starts with markup (`<`,
 *              an open tag or HTML, after a `#!` line, a byte order mark
 *              and white space) runs as a .php file does, its text
 *              outside the tags printed; any other source runs as PHP
 *              code, as `php -r` runs it.
 *   GuestExec  "path [args]": a .php file in the guest filesystem or a
 *              mount, with $argv and $argc set to the argv; an empty
 *              command runs /entrypoint.php, if the image has one.
 *   Call       (function, input): call `function`, any PHP callable
 *              named by a string (`greet`, `App\greet`,
 *              `Handlers::greet`), with `input` decoded from JSON
 *              (objects as arrays), and send what it returns back as
 *              JSON, the call's result.
 *
 * The embedder's host functions (SandboxBuilder::host_function) are
 * Hyperlight\call($name, ...$args): the arguments go to the host as a
 * JSON array, the result comes back decoded, and a host error is thrown
 * as a Hyperlight\HostException.
 *
 * A call's status is what the php CLI would exit with: exit()'s, or 255
 * for an uncaught exception or a fatal error, whose message goes to
 * standard error (log_errors on, display_errors off, the defaults below,
 * which a php.ini in /usr/local/etc/php overrides); a guest function that
 * did not return, an exception escaping it, fails with 255 too, taken by
 * the handler set_exception_handler() set or not (an exit() is its own
 * status, 0 a success).  An exception (a
 * parse error included) or an exit() leaves the request as it was for
 * the next call.  A fatal error (memory exhausted, a function declared
 * twice) ends the request, as it ends a script: PHP has marked every
 * object destructed and left the frames that were running as they were,
 * so the driver starts a new request, without what earlier calls
 * declared, once the functions registered with
 * register_shutdown_function() have run.  Those run then only: otherwise
 * the request never ends.  Any other bailout out of PHP (a write to the
 * output that fails, say) ends the request too.  An exception or exit()
 * does not bail out: the driver runs the code it calls from C (a guest
 * function, the exception handler, output callbacks, destructors) under
 * a frame, where they unwind as in a script.
 *
 * mt_rand(), rand(), shuffle() and the other functions on PHP's default
 * generator draw from a new getrandom() seed every call (reseeded at the
 * top of a call once the generator is in use), so every clone of a warm
 * snapshot draws its own sequence: PHP seeds that generator once a
 * request, and the snapshot would keep it.
 * A seed code gives mt_srand() or srand() stays, for the calls after
 * too, until code calls one of them without one; the mode it chose
 * (MT_RAND_PHP) stays either way.  OpenSSL's generator
 * gets new entropy every call for the same reason, and SQLite's is
 * reseeded.
 */

#include <sapi/embed/php_embed.h>
#include <SAPI.h>
#include <ext/json/php_json.h>
#include <ext/random/php_random.h>
#include <ext/spl/spl_exceptions.h>
#include <main/php_output.h>
#include <main/php_variables.h>
#include <zend_exceptions.h>
#include <zend_smart_str.h>

#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/random.h>
#include <sys/sysinfo.h>

#include <openssl/rand.h>
#include <sqlite3.h>

#include "hl_fc.h"
#include "hl_env.h"
#include "hl_driver.h"

#define NAME "hl_phpdriver"
#define ENTRYPOINT "/entrypoint.php"
#define EXEC_NAME "<exec>"
#define MAX_ARGS 64

/* What a host function call's arguments and a guest function's result
 * are encoded with: text as it is, and 1.0 kept a float. */
#define JSON_FLAGS (PHP_JSON_UNESCAPED_SLASHES | PHP_JSON_UNESCAPED_UNICODE | \
		    PHP_JSON_PRESERVE_ZERO_FRACTION)

/* ── JSON ──────────────────────────────────────────────────────── */

/* The message of the last encode or decode that failed, as
 * json_last_error_msg() gives it: the C API only has the code. */
static zend_string *json_error_message(void)
{
	zend_function *fn = zend_hash_str_find_ptr(CG(function_table),
						   ZEND_STRL("json_last_error_msg"));
	zval ret;

	/* Not there when disable_functions took it out, and not code's own
	 * function of that name then. */
	if (!fn || fn->type != ZEND_INTERNAL_FUNCTION)
		return zend_strpprintf(0, "JSON error %d", (int)JSON_G(error_code));
	ZVAL_UNDEF(&ret);
	zend_call_known_function(fn, NULL, NULL, &ret, 0, NULL, NULL);
	if (Z_TYPE(ret) == IS_STRING)
		return Z_STR(ret);
	zval_ptr_dtor(&ret);
	return zend_strpprintf(0, "JSON error %d", (int)JSON_G(error_code));
}

/* Whether the last encode failed: php_json_encode() reports a value it
 * cannot encode (NAN, a resource) in an array as a success, with '0' in
 * its place, as json_encode() with JSON_PARTIAL_OUTPUT_ON_ERROR does. */
static int json_failed(void)
{
	return JSON_G(error_code) != PHP_JSON_ERROR_NONE;
}

/* ── Host functions ────────────────────────────────────────────── */

static zend_class_entry *g_host_exception_ce;

/* The function of the frame the driver runs code from C under (see
 * push_frame()). */
static zend_internal_function g_frame_function;
/* JsonException: ext/json exports it, its header does not declare it. */
extern PHP_JSON_API zend_class_entry *php_json_exception_ce;

ZEND_BEGIN_ARG_WITH_RETURN_TYPE_INFO_EX(arginfo_hyperlight_call, 0, 1, IS_MIXED, 0)
	ZEND_ARG_TYPE_INFO(0, name, IS_STRING, 0)
	ZEND_ARG_VARIADIC_TYPE_INFO(0, args, IS_MIXED, 0)
ZEND_END_ARG_INFO()

/* Hyperlight\call(string $name, mixed ...$args): mixed */
static ZEND_FUNCTION(hyperlight_call)
{
	zend_string *name;
	zval *args = NULL, list;
	uint32_t argc = 0;
	HashTable *named = NULL;
	smart_str json = { 0 };
	const char *out;
	size_t out_len;
	int rc, saved_errno;
	/* What json_last_error() says, which the arguments' encoding must
	 * not change. */
	php_json_error_code saved_json_error;

	ZEND_PARSE_PARAMETERS_START(1, -1)
		Z_PARAM_STR(name)
		Z_PARAM_VARIADIC_WITH_NAMED(args, argc, named)
	ZEND_PARSE_PARAMETERS_END();

	/* The host takes a JSON array: there is no name to send. */
	if (named && zend_hash_num_elements(named)) {
		zend_throw_error(NULL, "Hyperlight\\call() takes no named arguments "
				 "for the host function");
		RETURN_THROWS();
	}

	/* The empty name asks the host for its list of functions. */
	if (!ZSTR_LEN(name)) {
		zend_argument_value_error(1, "must be a host function's name");
		RETURN_THROWS();
	}
	saved_json_error = JSON_G(error_code);

	array_init_size(&list, argc);
	for (uint32_t i = 0; i < argc; i++) {
		Z_TRY_ADDREF(args[i]);
		zend_hash_next_index_insert_new(Z_ARRVAL(list), &args[i]);
	}
	rc = php_json_encode(&json, &list, JSON_FLAGS);
	zval_ptr_dtor(&list);
	/* An argument's jsonSerialize() threw: the call is not made. */
	if (EG(exception)) {
		smart_str_free(&json);
		JSON_G(error_code) = saved_json_error;
		RETURN_THROWS();
	}
	if (rc == FAILURE || json_failed()) {
		zend_string *msg = json_error_message();

		smart_str_free(&json);
		zend_throw_exception_ex(php_json_exception_ce, JSON_G(error_code),
					"%s's arguments cannot be JSON: %s",
					ZSTR_VAL(name), ZSTR_VAL(msg));
		zend_string_release(msg);
		JSON_G(error_code) = saved_json_error;
		RETURN_THROWS();
	}
	smart_str_0(&json);
	JSON_G(error_code) = saved_json_error;

	rc = hl_host_call(ZSTR_VAL(name), ZSTR_LEN(name), ZSTR_VAL(json.s),
			  ZSTR_LEN(json.s), &out, &out_len);
	saved_errno = errno; /* before the free below can change it */
	smart_str_free(&json);
	if (rc < 0) {
		zend_throw_exception_ex(g_host_exception_ce, 0, "host function %s: %s",
					ZSTR_VAL(name), strerror(saved_errno));
		RETURN_THROWS();
	}
	if (rc > 0) {
		/* The whole message, a NUL in it included. */
		zend_object *ex = zend_throw_exception(g_host_exception_ce, NULL, 0);

		zend_update_property_stringl(zend_ce_exception, ex, "message",
					     sizeof("message") - 1, out, out_len);
		RETURN_THROWS();
	}
	if (!out_len)
		RETURN_NULL();
	/* Throws a JsonException when the host's reply is not JSON. */
	php_json_decode_ex(return_value, out, out_len,
			   PHP_JSON_OBJECT_AS_ARRAY | PHP_JSON_THROW_ON_ERROR,
			   PHP_JSON_PARSER_DEFAULT_DEPTH);
}

static const zend_function_entry hyperlight_functions[] = {
	ZEND_RAW_FENTRY("Hyperlight\\call", zif_hyperlight_call,
			arginfo_hyperlight_call, 0, NULL, NULL)
	ZEND_FE_END
};

static PHP_MINIT_FUNCTION(hyperlight)
{
	zend_class_entry ce;

	INIT_NS_CLASS_ENTRY(ce, "Hyperlight", "HostException", NULL);
	g_host_exception_ce = zend_register_internal_class_ex(&ce, spl_ce_RuntimeException);
	g_host_exception_ce->ce_flags |= ZEND_ACC_FINAL;
	/* Interned now, for the module's life: a string interned during a
	 * request is freed with it. */
	g_frame_function.type = ZEND_INTERNAL_FUNCTION;
	g_frame_function.function_name =
		zend_string_init_interned("{guest function call}",
					  sizeof("{guest function call}") - 1, 1);
	return SUCCESS;
}

static const zend_module_dep hyperlight_deps[] = {
	ZEND_MOD_REQUIRED("json")
	ZEND_MOD_REQUIRED("spl")
	ZEND_MOD_END
};

/* An extension of its own, so extension_loaded('hyperlight') says where
 * code runs. */
static zend_module_entry hyperlight_module = {
	STANDARD_MODULE_HEADER_EX, NULL, hyperlight_deps,
	"hyperlight",
	hyperlight_functions,
	PHP_MINIT(hyperlight),
	NULL, NULL, NULL, NULL,
	PHP_VERSION,
	STANDARD_MODULE_PROPERTIES
};

/* ── The engine ────────────────────────────────────────────────── */

static int start_module(sapi_module_struct *sapi)
{
	return php_module_startup(sapi, &hyperlight_module);
}

/* The lowest-precedence settings, under any php.ini: errors to standard
 * error (the embed SAPI logs there when error_log is unset) and not into
 * the output too, as the CLI prints them without a php.ini, so an
 * embedder reads them apart from what code prints; and memory limits a
 * script reaches before the guest runs out.  PHP 8.5 lets an allocation
 * past memory_limit through, raising "Allowed memory size" at the next
 * chance, up to a hard limit (twice memory_limit by default), past which
 * it exits with 137: memory_limit is two thirds of the memory free at
 * boot and the hard limit 112% of it, three quarters, the rest for what
 * PHP's libraries (libxml2, SQLite, OpenSSL) allocate outside them.
 * PHP's own 128M would let a script take all of a smaller guest and end
 * it.  OPcache off, as the CLI
 * has it: it caches a file for the next request, and here one request
 * lasts the guest's life, while its cache (128M) would grow past the
 * memory limit. */
static void ini_defaults(HashTable *configuration_hash)
{
	struct sysinfo si;
	char limit[32];
	zval v;
	/* Hard limit: memory_limit's 112%. */
	static const char hard[] = "112";

#define INI_DEFAULT(name, value, len) \
	ZVAL_NEW_STR(&v, zend_string_init(value, len, 1)); \
	zend_hash_str_update(configuration_hash, name, sizeof(name) - 1, &v)

	INI_DEFAULT("display_errors", "0", 1);
	INI_DEFAULT("log_errors", "1", 1);
	INI_DEFAULT("opcache.enable", "0", 1);
	if (sysinfo(&si) == 0) {
		int n = snprintf(limit, sizeof(limit), "%lluM",
				 (unsigned long long)((uint64_t)si.freeram * si.mem_unit / 3 * 2 >> 20));

		INI_DEFAULT("memory_limit", limit, (size_t)n);
		INI_DEFAULT("memory_hard_limit_percent", hard, sizeof(hard) - 1);
		/* And the most ini_set() can raise it to (PHP 8.5): code that
		 * sets -1, as Composer does, stays under it too.  A php.ini that
		 * raises memory_limit must raise this too. */
		INI_DEFAULT("max_memory_limit", limit, (size_t)n);
	}
#undef INI_DEFAULT
}

/* What the CLI defines and the embed SAPI does not: the standard streams
 * as constants, opened as cli_register_file_handles() opens them (not
 * through fopen(), which disable_functions may take out). */
static int define_stream(const char *name, const char *path, const char *mode)
{
	php_stream *stream = php_stream_open_wrapper_ex(path, mode, 0, NULL, NULL);
	zend_constant c;

	/* Outside the CLI php://stdin and the others are dup()s of 0, 1 and
	 * 2, closed with the request. */
	if (!stream)
		return -1;
	php_stream_to_zval(stream, &c.value);
	Z_CONSTANT_FLAGS(c.value) = 0;
	c.name = zend_string_init_interned(name, strlen(name), 0);
	return zend_register_constant(&c) ? 0 : -1;
}

/* What every request starts with. */
static int start_request(void)
{
	/* A .php file may start with a #! line, as the CLI runs it. */
	CG(skip_shebang) = 1;
	/* Startup is over: a warning names its function, not "PHP Request
	 * Startup", as php_execute_script() would have it. */
	PG(during_request_startup) = 0;
	/* Each one that opens, as the CLI defines them. */
	define_stream("STDIN", "php://stdin", "rb");
	define_stream("STDOUT", "php://stdout", "wb");
	define_stream("STDERR", "php://stderr", "wb");
	return 0;
}

static void (*g_php_error_cb)(int type, zend_string *file, const uint32_t line,
			      zend_string *message);

static zend_execute_data *push_frame(void);
static void pop_frame(zend_execute_data *frame);
static bool handle_uncaught(void);

/* Why the call ends the request, if it does. */
static enum {
	REQUEST_GOES_ON,
	FATAL_ERROR,
	BAILED_OUT,
} g_end_request;

/* Notes a fatal error, one php_error_cb bails out of (for these types,
 * unless told not to).  Before it does, it marks every object destructed,
 * so none of their destructors or a suspended fiber's unwinding will run,
 * and zend_bailout() leaves the frames running as they are, their
 * variables allocated and the script's globals pointing into them: the
 * request cannot go on as it was. */
static void note_error(int type, zend_string *file, const uint32_t line,
		       zend_string *message)
{
	switch (type & E_ALL) {
	case E_ERROR:
	case E_CORE_ERROR:
	case E_COMPILE_ERROR:
	case E_USER_ERROR:
	case E_RECOVERABLE_ERROR:
	case E_PARSE:
		if (!(type & E_DONT_BAIL))
			g_end_request = FATAL_ERROR;
		break;
	}
	g_php_error_cb(type, file, line, message);
}

/* mt_srand() and srand(), and whether code has seeded the default
 * generator with a seed of its own, which the next calls keep. */
static zif_handler g_mt_srand;
static bool g_seeded_by_code;

static ZEND_NAMED_FUNCTION(seed_mt)
{
	bool seeded = RANDOM_G(mt19937_seeded);

	/* mt_srand() sets the flag once it has seeded, an exception after
	 * (a deprecation an error handler threw) or not; one before (a bad
	 * argument) seeds nothing. */
	RANDOM_G(mt19937_seeded) = false;
	g_mt_srand(INTERNAL_FUNCTION_PARAM_PASSTHRU);
	if (RANDOM_G(mt19937_seeded))
		g_seeded_by_code = ZEND_NUM_ARGS() > 0 &&
				   Z_TYPE_P(ZEND_CALL_ARG(execute_data, 1)) != IS_NULL;
	else
		RANDOM_G(mt19937_seeded) = seeded;
}

/* Put `wrapper` in place of the internal function `name`'s handler. */
static void wrap(const char *name, zif_handler wrapper, zif_handler *original)
{
	zend_function *f = zend_hash_str_find_ptr(CG(function_table), name, strlen(name));

	/* One disable_functions took out is not there to wrap. */
	if (!f || f->type != ZEND_INTERNAL_FUNCTION)
		return;
	*original = f->internal_function.handler;
	f->internal_function.handler = wrapper;
}

static void wrap_functions(void)
{
	/* srand() is mt_srand()'s alias: the same handler. */
	wrap("mt_srand", seed_mt, &g_mt_srand);
	wrap("srand", seed_mt, &g_mt_srand);
}

static int start_engine(void)
{
	static char *argv[] = { NAME, NULL };

	php_embed_module.startup = start_module;
	php_embed_module.ini_defaults = ini_defaults;
	/* php.ini from /usr/local/etc/php only, not the working directory. */
	php_embed_module.php_ini_ignore_cwd = 1;
	if (php_embed_init(1, argv) == FAILURE)
		return -1;
	g_php_error_cb = zend_error_cb;
	zend_error_cb = note_error;
	wrap_functions();
	return start_request();
}

/* End the request and start another, as php_embed_init() started the
 * first: the request's memory is freed with it, what abandoned frames
 * held included.  The memory manager keeps the chunks the request used
 * for the next one and counts them against memory_limit, which a large
 * string or array would then run into: a limit of one chunk returns them
 * to the guest. */
static int restart_request(void)
{
	php_request_shutdown(NULL);
	zend_set_memory_limit(ZEND_MM_CHUNK_SIZE);
	zend_set_memory_limit(PG(memory_limit));
	if (php_request_startup() == FAILURE)
		return -1;
	/* A new request seeds the generator anew (the random extension's
	 * RINIT). */
	g_seeded_by_code = false;
	SG(headers_sent) = 1;
	SG(request_info).no_headers = 1;
	return start_request();
}

/* ── Globals a call sees ───────────────────────────────────────── */

/* $GLOBALS[name], or NULL when it is not an array. */
static zval *global_array(const char *name, size_t len)
{
	zval *arr = zend_hash_str_find_deref(&EG(symbol_table), name, len);

	if (!arr || Z_TYPE_P(arr) != IS_ARRAY)
		return NULL;
	SEPARATE_ARRAY(arr);
	return arr;
}

/* Whether what the driver did to set a call up (a value replaced, whose
 * destructor ran) threw or exit()ed, which stops the call before its code
 * runs: with the status of an uncaught exception, even when the handler
 * set_exception_handler() set took it, or the exit()'s. */
static bool setup_stopped(void)
{
	bool threw = EG(exception) != NULL;
	bool exited = handle_uncaught();

	if (threw && !exited && !EG(exit_status))
		EG(exit_status) = 255;
	return threw || exited || EG(exit_status);
}

/* $GLOBALS[array][key] = v, which it takes, if that array is there.
 * Looked up for each one: the destructor of a value replaced may move
 * the globals, or the array. */
static void set_global(const char *array, const char *key, zval *v)
{
	zval *arr = global_array(array, strlen(array));

	if (arr)
		zend_symtable_str_update(Z_ARRVAL_P(arr), key, strlen(key), v);
	else
		zval_ptr_dtor(v);
}

/* $_SERVER and $_ENV are filled from the environment when code first
 * uses them, so a variable the host changes later is set there too:
 * getenv() reads environ, which hl_env_refresh() has set. */
/* Which of $_SERVER and $_ENV take the environment: variables_order's S
 * and E, as PHP fills them. */
struct env_targets {
	bool server, env;
};

static void set_env_var(const char *key, const char *val, void *ctx)
{
	const struct env_targets *to = ctx;
	zend_string *value;
	zval v;

	/* Names PHP leaves out of them, as valid_environment_name() does at
	 * request startup. */
	if (strpbrk(key, " .["))
		return;
	value = zend_string_init(val, strlen(val), 0);

	if (to->server) {
		ZVAL_STR_COPY(&v, value);
		set_global("_SERVER", key, &v);
	}
	if (to->env) {
		ZVAL_STR_COPY(&v, value);
		set_global("_ENV", key, &v);
	}
	zend_string_release(value);
}

static const struct {
	const char *name;
	int track;
} g_track_vars[] = { { "_SERVER", TRACK_VARS_SERVER }, { "_ENV", TRACK_VARS_ENV } };

static void unshare_track_vars(void)
{
	for (size_t i = 0; i < sizeof(g_track_vars) / sizeof(g_track_vars[0]); i++) {
		zval *arr = &PG(http_globals)[g_track_vars[i].track];

		zval_ptr_dtor(arr);
		ZVAL_EMPTY_ARRAY(arr);
	}
}

/* PHP keeps its own reference to $_SERVER and $_ENV (filter_input()
 * reads it for INPUT_ENV, getopt() for $_SERVER's argv), which writing
 * to the globals separated from: share them again.  INPUT_SERVER is the
 * filter's own copy, which the embed SAPI does not fill. */
static void share_track_vars(void)
{
	for (size_t i = 0; i < sizeof(g_track_vars) / sizeof(g_track_vars[0]); i++) {
		zval *arr = zend_hash_str_find_deref(&EG(symbol_table), g_track_vars[i].name,
						     strlen(g_track_vars[i].name));

		if (arr && Z_TYPE_P(arr) == IS_ARRAY) {
			zval_ptr_dtor(&PG(http_globals)[g_track_vars[i].track]);
			ZVAL_COPY(&PG(http_globals)[g_track_vars[i].track], arr);
		}
	}
}

/* The call's command line: the file and its arguments for GuestExec,
 * EXEC_NAME for Exec.  The request keeps a pointer to it
 * (SG(request_info).argv), which $_SERVER is built from when code first
 * uses it, so it outlives the call. */
static char g_line[4096];
static char *g_argv[MAX_ARGS];

/* $argv and $argc, in the globals and in $_SERVER, and the script's name
 * there, as the CLI sets them, under a frame: the destructor of a value
 * replaced may throw or exit(), which stops the call (true). */
static bool set_script_args(void)
{
	static const char *const names[] = { "SCRIPT_FILENAME", "SCRIPT_NAME", "PHP_SELF" };
	zend_execute_data *frame = push_frame();
	bool stopped;

	/* SG(request_info) has the command line: serve() set it. */
	unshare_track_vars();
	zend_is_auto_global_str(ZEND_STRL("_SERVER"));
	/* Whatever register_argc_argv says, as the CLI forces it on; the
	 * globals here, $_SERVER after, each looked up again. */
	php_build_argv(NULL, NULL);
	for (size_t i = 0; i < 2; i++) {
		const char *name = i ? "argc" : "argv";
		zval *g = zend_hash_str_find(&EG(symbol_table), name, strlen(name));
		zval v;

		if (g) {
			ZVAL_COPY_DEREF(&v, g);
			set_global("_SERVER", name, &v);
		}
	}
	for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
		zval v;

		ZVAL_STRING(&v, g_argv[0]);
		set_global("_SERVER", names[i], &v);
	}
	share_track_vars();
	stopped = setup_stopped();
	pop_frame(frame);
	return stopped;
}

/* ── Running a call ────────────────────────────────────────────── */

/* A frame for code the driver runs from C: an internal function's, as
 * array_map() has when it calls back into PHP.  Without one, an exception
 * or exit() that reaches the top bails out of PHP, past what PHP was in
 * the middle of; under it, they unwind as in any script and come back in
 * EG(exception).  Not under a script, which takes its globals from the
 * nearest user frame: around the guest function call and the exception
 * handler. */
static zend_execute_data *push_frame(void)
{
	zend_execute_data *frame = zend_vm_stack_push_call_frame(
		ZEND_CALL_TOP_FUNCTION, (zend_function *)&g_frame_function, 0, NULL);

	frame->prev_execute_data = EG(current_execute_data);
	EG(current_execute_data) = frame;
	return frame;
}

static void pop_frame(zend_execute_data *frame)
{
	EG(current_execute_data) = frame->prev_execute_data;
	zend_vm_stack_free_call_frame(frame);
}

/* An "Allowed memory size" error PHP 8.5 left pending (it lets the
 * allocation through and raises the error at the next chance): raised
 * here, for the call that crossed the limit, not the next one. */
static void raise_delayed_oom(void)
{
	if (ZEND_HAS_OOM())
		zend_handle_delayed_oom();
}

/* The end of a script with an exception left uncaught (EG(exception)
 * set, under a frame): an exit() is done; another goes to the handler
 * set_exception_handler() set, as zend_user_exception_handler() hands it
 * over, and else, or when the handler throws, is reported, the status
 * 255.  Whether it ended in an exit(), the handler's included.  Not
 * zend_user_exception_handler() itself, which drops an exception the
 * handler throws without a word: here it is reported, as it is at the end
 * of a CLI script, where nothing can catch it. */
static bool handle_uncaught(void)
{
	if (EG(exception) && !zend_is_unwind_exit(EG(exception)) &&
	    Z_TYPE(EG(user_exception_handler)) != IS_UNDEF) {
		zend_object *ex = EG(exception);
		int depth = zend_stack_count(&EG(user_exception_handlers));
		zval handler, param, ret;

		EG(exception) = NULL;
		ZVAL_OBJ(&param, ex);
		/* Set aside while it runs, so an exception it throws is not
		 * its own, and put back after unless it set another. */
		ZVAL_COPY_VALUE(&handler, &EG(user_exception_handler));
		zend_stack_push(&EG(user_exception_handlers), &handler);
		ZVAL_UNDEF(&EG(user_exception_handler));
		ZVAL_UNDEF(&ret);
		if (call_user_function(NULL, NULL, &handler, &ret, 1, &param) == SUCCESS) {
			zval_ptr_dtor(&ret);
			OBJ_RELEASE(ex);
		} else if (!EG(exception)) {
			/* Not called: the exception is reported as it is. */
			EG(exception) = ex;
		} else {
			OBJ_RELEASE(ex);
		}
		/* The handler back, as zend_user_exception_handler() puts it,
		 * unless it changed the handlers itself (set or cleared one,
		 * which pushes onto the stack, or restored one, which pops our
		 * entry): then what it left stands.  And the stack back as it
		 * was: PHP leaves the entries pushed, at the end of a script,
		 * where this request goes on. */
		if (zend_stack_count(&EG(user_exception_handlers)) == depth + 1 &&
		    Z_TYPE(EG(user_exception_handler)) == IS_UNDEF) {
			zval *top = zend_stack_top(&EG(user_exception_handlers));

			ZVAL_COPY_VALUE(&EG(user_exception_handler), top);
			zend_stack_del_top(&EG(user_exception_handlers));
		}
		while (zend_stack_count(&EG(user_exception_handlers)) > depth) {
			zval_ptr_dtor(zend_stack_top(&EG(user_exception_handlers)));
			zend_stack_del_top(&EG(user_exception_handlers));
		}
	}
	/* Each in turn: one whose __toString() throws is reported, and
	 * leaves that one behind. */
	while (EG(exception)) {
		if (zend_is_unwind_exit(EG(exception))) {
			OBJ_RELEASE(EG(exception));
			EG(exception) = NULL;
			return true;
		}
		zend_exception_error(EG(exception), E_ERROR);
	}
	return false;
}

/* Run a compiled script and free it, as zend_execute_script() does. */
static void run_op_array(zend_op_array *op_array)
{
	zend_execute_data *frame;

	/* Not when compiling it threw (an error handler's exception). */
	if (op_array && !EG(exception)) {
		zend_execute(op_array, NULL);
		raise_delayed_oom();
		zend_exception_restore();
	}
	frame = push_frame();
	/* An exception left uncaught.  A parse error is the script's own
	 * failure, which no handler sees, as in zend_execute_script(). */
	if (EG(exception) && !op_array)
		zend_exception_error(EG(exception), E_ERROR);
	else
		handle_uncaught();
	if (op_array) {
		/* Its statics' destructors may throw. */
		zend_destroy_static_vars(op_array);
		destroy_op_array(op_array);
		efree_size(op_array, sizeof(zend_op_array));
		handle_uncaught();
	}
	pop_frame(frame);
}

/* Compile and run source as eval() does, from `position`: an open tag
 * first, or straight PHP. */
static void run_source(const char *src, size_t len, zend_compile_position position)
{
	zend_string *code = zend_string_init(src, len, 0);
	/* Under a frame: an error handler may throw at a compile-time
	 * warning. */
	zend_execute_data *frame = push_frame();
	zend_op_array *op_array = zend_compile_string(code, EXEC_NAME, position);

	pop_frame(frame);
	zend_string_release(code);
	run_op_array(op_array);
}

/* Whether source runs as a file: markup first, an open tag or HTML,
 * after a #! line, a byte order mark and white space.  PHP code cannot
 * start with `<` but for a heredoc's `<<<`. */
static int starts_with_markup(const char *src, size_t len)
{
	const char *p = src, *end = src + len;

	/* A byte order mark, before a #! line or after it. */
	if (end - p >= 3 && !memcmp(p, "\xef\xbb\xbf", 3))
		p += 3;
	if (end - p >= 2 && p[0] == '#' && p[1] == '!') {
		while (p < end && *p != '\n')
			p++;
		if (p < end)
			p++;
	}
	if (end - p >= 3 && !memcmp(p, "\xef\xbb\xbf", 3))
		p += 3;
	while (p < end && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n'))
		p++;
	return p < end && *p == '<' && !(end - p >= 3 && p[1] == '<' && p[2] == '<');
}

/* The script run_file() opened, until PHP has it. */
static FILE *g_script;

static int run_file(void)
{
	char path[PATH_MAX];
	zend_file_handle fh;
	zend_execute_data *frame;
	zend_op_array *op_array;
	struct stat st;
	FILE *fp;
	int c;

	/* Opened here and handed to PHP, as the CLI opens its script: by its
	 * path, not through include_path, and not under open_basedir, which
	 * is for what the script opens.  Read too, a regular file (a pipe's
	 * byte would be the script's): a mounted file opens before the host
	 * checks that it may be read. */
	fp = realpath(g_argv[0], path) ? fopen(path, "rb") : NULL;
	if (fp && (fstat(fileno(fp), &st) < 0 || S_ISDIR(st.st_mode) ||
		   (S_ISREG(st.st_mode) &&
		    ((c = getc(fp)) == EOF ? ferror(fp) : ungetc(c, fp) == EOF)))) {
		fclose(fp);
		fp = NULL;
	}
	/* The CLI's own message and status for a script it cannot open. */
	if (!fp) {
		fprintf(stderr, "Could not open input file: %s\n", g_argv[0]);
		return 1;
	}
	/* Closed by php_dispatch() if setting the call up bails out first. */
	g_script = fp;
	if (set_script_args()) {
		fclose(fp);
		g_script = NULL;
		return 0;
	}
	g_script = NULL;
	/* Not php_execute_script(), whose own zend_try would keep a bailout
	 * from php_dispatch(), nor its auto_prepend_file and
	 * auto_append_file, which Exec does not run either. */
	zend_stream_init_fp(&fh, fp, path);
	fh.primary_script = 1;
	frame = push_frame();
	op_array = zend_compile_file(&fh, ZEND_REQUIRE);
	pop_frame(frame);
	/* Included once it compiled: a script fixed after a parse error can
	 * be required again. */
	if (op_array)
		zend_hash_str_add_empty_element(&EG(included_files), path, strlen(path));
	zend_destroy_file_handle(&fh);
	run_op_array(op_array);
	return 0;
}

/* The status of a guest function call that an exception escaped (an
 * exit()'s, or 255), once it is handled as uncaught. */
static int escaped(const char *name, size_t name_len, const char *where)
{
	if (handle_uncaught())
		return (int)(EG(exit_status) & 0xff);
	/* Reported as uncaught (255), or taken by the handler. */
	if (!EG(exit_status))
		fprintf(stderr, "%s: an exception escaped %s %.*s\n", NAME, where,
			(int)name_len, name);
	return 255;
}

/* Call `name` with `input` decoded from JSON, under a frame; what it
 * returns, encoded, is the call's result.  0; 1 when the call cannot be
 * made or its result cannot be sent; an exception's status, as escaped()
 * gives it, when one escapes the function, the encoding of its result
 * (jsonSerialize()) or the destruction of what the call held. */
static int call_in_frame(const char *name, size_t name_len, const char *input,
			 size_t input_len)
{
	php_json_error_code json_error = JSON_G(error_code);
	zend_fcall_info_cache fcc;
	bool have_fcc = false;
	zval callable, arg, result;
	smart_str json = { 0 };
	char *error = NULL;
	int status = 0;

	ZVAL_UNDEF(&arg);
	ZVAL_UNDEF(&result);
	ZVAL_STRINGL(&callable, name, name_len);
	/* An autoloader may run, and throw, as the name is looked up. */
	if (!zend_is_callable_ex(&callable, NULL, 0, NULL, &fcc, &error)) {
		if (EG(exception)) {
			status = escaped(name, name_len, "the lookup of");
		} else {
			fprintf(stderr, "%s: no function %.*s: %s\n", NAME, (int)name_len,
				name, error ? error : "not callable");
			status = 1;
		}
		if (error)
			efree(error);
		goto out;
	}
	if (error)
		efree(error);
	/* A __callStatic() trampoline is the caller's to free, called or
	 * not. */
	have_fcc = true;

	/* Empty input is no argument, not one null.  Copied: the scanner
	 * reads to a NUL, which hl_fc.h does not promise after it. */
	if (input_len) {
		zend_string *in = zend_string_init(input, input_len, 0);
		/* Thrown, so json_last_error() is left as it was. */
		zend_result decoded = php_json_decode_ex(&arg, ZSTR_VAL(in), ZSTR_LEN(in),
							 PHP_JSON_OBJECT_AS_ARRAY |
							 PHP_JSON_THROW_ON_ERROR,
							 PHP_JSON_PARSER_DEFAULT_DEPTH);

		zend_string_release(in);
		if (decoded == FAILURE) {
			zval rv;
			zval *msg = EG(exception) ?
				zend_read_property_ex(zend_ce_exception, EG(exception),
						      ZSTR_KNOWN(ZEND_STR_MESSAGE), 1, &rv) : NULL;

			fprintf(stderr, "%s: the input to %.*s is not JSON: %s\n", NAME,
				(int)name_len, name,
				msg && Z_TYPE_P(msg) == IS_STRING ? Z_STRVAL_P(msg) : "?");
			zend_clear_exception();
			status = 1;
			goto out;
		}
	}

	zend_call_known_fcc(&fcc, &result, input_len ? 1 : 0, input_len ? &arg : NULL, NULL);
	raise_delayed_oom();
	/* What the function left json_last_error() at is what stays. */
	json_error = JSON_G(error_code);
	if (EG(exception)) {
		status = escaped(name, name_len, "the call of");
		goto out;
	}

	/* null, a void function's included, is no result. */
	if (Z_TYPE(result) != IS_UNDEF && Z_TYPE(result) != IS_NULL) {
		php_json_encode(&json, &result, JSON_FLAGS);
		if (EG(exception)) {
			status = escaped(name, name_len, "encoding the result of");
		} else if (json_failed() || !json.s) {
			zend_string *msg = json_error_message();

			fprintf(stderr, "%s: %.*s's result cannot be JSON: %s\n", NAME,
				(int)name_len, name, ZSTR_VAL(msg));
			zend_string_release(msg);
			status = 1;
		} else if (hl_set_result(ZSTR_VAL(json.s), ZSTR_LEN(json.s)) < 0) {
			fprintf(stderr, "%s: %.*s's result does not fit a call\n",
				NAME, (int)name_len, name);
			status = 1;
		}
	}

out:
	if (have_fcc)
		zend_release_fcall_info_cache(&fcc);
	smart_str_free(&json);
	zval_ptr_dtor(&result);
	zval_ptr_dtor(&arg);
	zval_ptr_dtor(&callable);
	/* A destructor of the result or the input threw. */
	if (EG(exception)) {
		int s = escaped(name, name_len, "the destruction of what was passed to and from");

		status = status ? status : s;
	}
	JSON_G(error_code) = json_error;
	return status;
}

static int call_function(const char *name, size_t name_len, const char *input,
			 size_t input_len)
{
	zend_execute_data *frame = push_frame();
	int status = call_in_frame(name, name_len, input, input_len);

	pop_frame(frame);
	return status;
}

struct call {
	const uint8_t *fc;
	size_t fc_len;
	int status;	/* the driver's own: -1 when it cannot run the call */
};

static void serve(struct call *c)
{
	size_t len;
	const char *arg = fc_arg0_string(c->fc, c->fc_len, &len);

	if (fc_name_is(c->fc, c->fc_len, "Call")) {
		size_t in_len;
		const char *in = fc_arg_string(c->fc, c->fc_len, 1, &in_len);

		if (!arg || !len || !in) {
			fprintf(stderr, "%s: a guest function call needs a function's name "
				"and an input\n", NAME);
			c->status = -1;
			return;
		}
		c->status = call_function(arg, len, in, in_len);
		return;
	}

	if (fc_name_is(c->fc, c->fc_len, "GuestExec")) {
		int argc;
		size_t n = arg ? len : 0;

		if (n >= sizeof(g_line)) {
			fprintf(stderr, "%s: the command line is longer than %zu bytes\n",
				NAME, sizeof(g_line) - 1);
			c->status = -1;
			return;
		}
		if (n)
			memcpy(g_line, arg, n);
		g_line[n] = '\0';
		argc = hl_split_ws(g_line, g_argv, MAX_ARGS);
		/* No command, or white space alone: the image's entry point,
		 * when it has one, as the other drivers run theirs. */
		if (!argc) {
			if (access(ENTRYPOINT, F_OK)) {
				fprintf(stderr, "hl: no %s in rootfs; nothing to run\n",
					ENTRYPOINT);
				/* g_argv was split empty: no command line now. */
				SG(request_info).argc = 0;
				return;
			}
			strcpy(g_line, ENTRYPOINT);
			argc = hl_split_ws(g_line, g_argv, MAX_ARGS);
		}
		/* In step with g_argv at once: a new request's startup builds
		 * $argv from them, whether this call runs or not. */
		SG(request_info).argc = argc;
		SG(request_info).argv = g_argv;
		c->status = run_file();
		return;
	}

	if (!arg) {
		fprintf(stderr, "%s: the call carries no source\n", NAME);
		c->status = -1;
		return;
	}
	strcpy(g_line, EXEC_NAME);
	g_argv[0] = g_line;
	g_argv[1] = NULL;
	SG(request_info).argc = 1;
	SG(request_info).argv = g_argv;
	if (set_script_args())
		return;
	run_source(arg, len, starts_with_markup(arg, len) ?
		   ZEND_COMPILE_POSITION_AT_SHEBANG : ZEND_COMPILE_POSITION_AFTER_OPEN_TAG);
}

/* What the call sees of the host and of the time: the environment, and
 * the request's time, which a warm snapshot would otherwise keep. */
static bool start_call(void)
{
	unsigned char seed[32];
	struct env_targets to;
	bool stopped;
	zval v;
	/* A value replaced may have a destructor, which may throw or exit():
	 * that stops the call (true). */
	zend_execute_data *frame = push_frame();

	/* The generators a warm snapshot would clone draw anew, before the
	 * setup below, which can run a destructor that draws (a value it
	 * replaces in $_SERVER/$_ENV, an output handler): OpenSSL
	 * (openssl_random_pseudo_bytes(), TLS) with new entropy, SQLite
	 * (random(), randomblob()), PHP's mt19937 unless code chose its seed
	 * (letting PHP seed it at the first draw would set the mode back),
	 * and the combined LCG at its next draw. */
	if (getrandom(seed, sizeof(seed), 0) == sizeof(seed))
		RAND_seed(seed, sizeof(seed));
	sqlite3_randomness(0, NULL);
	if (!g_seeded_by_code && RANDOM_G(mt19937_seeded))
		php_random_mt19937_seed_default(&RANDOM_G(mt19937));
	RANDOM_G(combined_lcg_seeded) = false;

	/* PHP's own reference to $_SERVER and $_ENV is dropped first, so the
	 * writes below do not copy them, and shared again after. */
	unshare_track_vars();
	to.server = strpbrk(PG(variables_order), "Ss") != NULL;
	to.env = strpbrk(PG(variables_order), "Ee") != NULL;
	hl_env_refresh(to.server || to.env ? set_env_var : NULL, &to);
	/* The output handler php.ini names, which a request starts and the
	 * end of the last call took down. */
	if (PG(output_handler) && PG(output_handler)[0] && !php_output_get_level()) {
		zval handler;

		ZVAL_STRING(&handler, PG(output_handler));
		php_output_start_user(&handler, 0, PHP_OUTPUT_HANDLER_STDFLAGS);
		zval_ptr_dtor(&handler);
	}
	SG(global_request_time) = 0;
	ZVAL_LONG(&v, (zend_long)sapi_get_request_time());
	set_global("_SERVER", "REQUEST_TIME", &v);
	ZVAL_DOUBLE(&v, sapi_get_request_time());
	set_global("_SERVER", "REQUEST_TIME_FLOAT", &v);
	share_track_vars();
	stopped = setup_stopped();
	pop_frame(frame);
	return stopped;
}

/* Drop the output handlers after one bailed out, before the request
 * ends: freeing one may run a destructor, which may bail out too. */
static void drop_output(void)
{
	zend_try {
		php_output_deactivate();
	} zend_end_try();
}

static int php_dispatch(const uint8_t *fc, size_t fc_len)
{
	/* Not a local: serve() sets its status, and a local changed after
	 * zend_try's setjmp() is undefined after the longjmp() back. */
	static struct call c;
	int status;

	c = (struct call){ fc, fc_len, 0 };
	EG(exit_status) = 0;
	g_end_request = REQUEST_GOES_ON;

	zend_try {
		if (!start_call())
			serve(&c);
		raise_delayed_oom();
	} zend_catch {
		/* A fatal error, or another bailout (a failed write to the
		 * output): PHP is left mid-way, and the request ends below. */
		if (!g_end_request)
			g_end_request = BAILED_OUT;
	} zend_end_try();

	/* The end of the script: the output buffers it left are flushed, as
	 * at the end of a request; when the request ends,
	 * php_request_shutdown() flushes them, after the shutdown functions,
	 * whose output they take. */
	if (!g_end_request) {
		zend_try {
			/* Under a frame: an output callback may throw or exit(). */
			zend_execute_data *frame = push_frame();

			php_output_end_all();
			handle_uncaught();
			pop_frame(frame);
			raise_delayed_oom();
			sapi_flush();
		} zend_catch {
			/* An output callback bailed out: the buffers, and the
			 * handler left running, are dropped. */
			if (!g_end_request)
				g_end_request = BAILED_OUT;
			drop_output();
		} zend_end_try();
	}
	if (g_end_request) {
		static const char *const why[] = {
			[FATAL_ERROR] = "the fatal error ended the PHP request",
			[BAILED_OUT] = "PHP bailed out of the call (a write to the "
				       "output failed, say), which ended the PHP request",
		};

		if (g_script) {
			fclose(g_script);
			g_script = NULL;
		}
		fprintf(stderr, "%s: %s: the next call starts a new one, without what "
			"earlier calls declared\n", NAME, why[g_end_request]);
		if (restart_request()) {
			fprintf(stderr, "%s: cannot start a new PHP request\n", NAME);
			fflush(stderr);
			_exit(1);
		}
	}
	fflush(stdout);
	fflush(stderr);

	/* exit()'s status as a process's: its low eight bits.  A bailout
	 * that ended the request without one (a failed write) is a failure
	 * all the same; after a fatal error, a shutdown function's exit()
	 * stands, as in the CLI. */
	status = c.status ? c.status : (int)(EG(exit_status) & 0xff);
	if (!status && g_end_request == BAILED_OUT)
		status = 255;
	EG(exit_status) = 0;
	return status;
}

/* ── Entry point ───────────────────────────────────────────────── */

int main(void)
{
	if (hl_driver_init(NAME))
		return 1;
	if (start_engine()) {
		fprintf(stderr, "%s: cannot start PHP\n", NAME);
		return 1;
	}
	hl_driver_serve_calls();
	/* Serve named calls from the kernel's queue; never returns. */
	hl_driver_run(php_dispatch);
}
