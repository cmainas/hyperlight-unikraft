// The runtime's random generators, seeded anew after a snapshot restore.
//
// Random.Shared and OpenSSL (RandomNumberGenerator) keep state in the
// process, seeded once, and a snapshot captures it: every guest restored
// from it would draw the same numbers.  The driver says when the guest was
// restored ('R', drivers/hl_child.h), and this seeds them again from the
// kernel's generator, which the kernel reseeds on every restore
// (docs/random.md).  new Random() and Guid.NewGuid() read the kernel's
// each time, so need nothing.

#nullable enable

using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

namespace Hyperlight;

static class Reseed
{
    const int RTLD_NOW = 2;
    const int RTLD_NOLOAD = 4;

    // musl answers for "libc.so" itself.
    [DllImport("libc", EntryPoint = "dlopen")]
    static extern nint DlOpen(string? file, int mode);

    [DllImport("libc", EntryPoint = "dlsym")]
    static extern nint DlSym(nint handle, string name);

    [DllImport("libc", EntryPoint = "dlclose")]
    static extern int DlClose(nint handle);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void RandSeed(byte[] buf, int num);

    // Random.Shared's per-thread state, found by Prepare().
    static FieldInfo? s_threadRandom;

    // Before the snapshot: the reflection, P/Invoke and marshalling stubs
    // cost most the first time, and Run() is on the first call of every
    // restored guest.
    public static void Prepare()
    {
        try
        {
            s_threadRandom = typeof(Random).GetNestedType("ThreadSafeRandom", BindingFlags.NonPublic)
                ?.GetField("t_random", BindingFlags.NonPublic | BindingFlags.Static);
            if (s_threadRandom == null)
                Console.Error.WriteLine("hl_dotnet_dispatch: Random.Shared's state not found; it will not be reseeded");
            // Once now, so the stubs it needs are in the snapshot: what it
            // reseeds is reseeded again after a restore.
            RunUnchecked();
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"hl_dotnet_dispatch: cannot prepare the reseed: {e.Message}");
            Console.Error.Flush();
        }
    }

    // A reseed that fails is reported, not the call's failure: the call
    // runs, as it would have before this existed.
    public static void Run()
    {
        try
        {
            RunUnchecked();
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"hl_dotnet_dispatch: cannot reseed: {e.Message}");
            Console.Error.Flush();
        }
    }

    static void RunUnchecked()
    {
        // Random.Shared's state is each thread's, made on its first use.
        // Calls run on this thread: dropping its state makes the next draw
        // seed a new one from the kernel.
        s_threadRandom?.SetValue(null, null);

        // OpenSSL, if the runtime has loaded it: its DRBG mixes the seed in
        // and reseeds the generators drawn from it.
        nint crypto = DlOpen("libcrypto.so.3", RTLD_NOW | RTLD_NOLOAD);
        if (crypto == 0)
            crypto = DlOpen("libcrypto.so.1.1", RTLD_NOW | RTLD_NOLOAD);
        if (crypto == 0)
            return;
        try
        {
            nint seed = DlSym(crypto, "RAND_seed");
            if (seed == 0)
                return;
            var bytes = new byte[32];
            using (var urandom = File.OpenRead("/dev/urandom"))
                urandom.ReadExactly(bytes);
            Marshal.GetDelegateForFunctionPointer<RandSeed>(seed)(bytes, bytes.Length);
        }
        finally
        {
            DlClose(crypto);
        }
    }
}
