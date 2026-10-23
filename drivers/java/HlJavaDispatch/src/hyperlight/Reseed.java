// The JVM's random generators, seeded anew after a snapshot restore.
//
// The JVM seeds them once, from the clock, and a snapshot captures them
// with the rest of the heap: every guest restored from it would draw the
// same numbers.  The driver says when the guest was restored ('R',
// drivers/hl_child.h) and this gives each one a seed from the kernel's
// generator, which the kernel reseeds on every restore (docs/random.md).
// The jar's manifest opens the packages it reaches into.

package hyperlight;

import java.io.FileInputStream;
import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.security.DrbgParameters;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.concurrent.atomic.AtomicLong;

final class Reseed {
    private Reseed() {}

    /** The static AtomicLong each class seeds its new instances from:
     *  `new Random()`, `new SplittableRandom()` and each RandomGenerator
     *  algorithm (RandomGenerator.getDefault() and .of(name)). */
    private static final String[][] SEED_SOURCES = {
        { "java.util.Random", "seedUniquifier" },
        { "java.util.SplittableRandom", "defaultGen" },
        { "java.util.concurrent.ThreadLocalRandom", "seeder" },
        { "jdk.internal.random.L32X64MixRandom", "defaultGen" },
        { "jdk.internal.random.L64X128MixRandom", "defaultGen" },
        { "jdk.internal.random.L64X128StarStarRandom", "defaultGen" },
        { "jdk.internal.random.L64X256MixRandom", "defaultGen" },
        { "jdk.internal.random.L64X1024MixRandom", "defaultGen" },
        { "jdk.internal.random.L128X128MixRandom", "defaultGen" },
        { "jdk.internal.random.L128X256MixRandom", "defaultGen" },
        { "jdk.internal.random.L128X1024MixRandom", "defaultGen" },
        { "jdk.internal.random.Xoroshiro128PlusPlus", "defaultGen" },
        { "jdk.internal.random.Xoshiro256PlusPlus", "DEFAULT_GEN" },
    };

    /** The Random instances the JDK keeps: Math.random()'s and
     *  StrictMath.random()'s, made with their holder class. */
    private static final String[][] SHARED_RANDOMS = {
        { "java.lang.Math$RandomNumberGeneratorHolder", "randomNumberGenerator" },
        { "java.lang.StrictMath$RandomNumberGeneratorHolder", "randomNumberGenerator" },
    };

    // Found by prepare(), before the snapshot: reflection is slow the first
    // time each field is read, and run() is on the first call of every
    // restored guest.  The fields are final, so their objects stay.
    private static final List<AtomicLong> GENERATORS = new ArrayList<>();
    private static final List<Random> RANDOMS = new ArrayList<>();
    // Collections.shuffle(list)'s Random, made on its first use.
    private static Field collectionsRandom;
    private static Method threadLocalInit;
    // UUID.randomUUID()'s SecureRandom, made on its first use: a DRBG
    // keeps its state.  NativePRNG's readers (the default, the blocking
    // and the non-blocking one) XOR /dev/urandom into each draw but reuse
    // the bytes they read for up to 100 ms, which a guest restored that
    // soon would reuse.  Each is reached only if its class was initialized
    // (the code used it), and only in run(): initializing these classes
    // before the snapshot changes which SecureRandom the JDK picks.
    private static Field uuidRandom;
    // What every DRBG reseeds and instantiates from: a HashDrbg, held by
    // the seeder lambda, cloned with the rest once a DRBG was used.
    private static Field drbgSeeder;
    private static Method drbgReseed;
    // What every SHA1PRNG seeds itself from on its first draw.
    private static Field sha1Seeder;
    private static Method sha1SetSeed;
    private static final List<Field> NATIVE_READERS = new ArrayList<>();
    private static Field nativeBuffered;
    // jdk.internal.misc.Unsafe and its shouldBeInitialized(Class), which
    // the manifest exports to us; without them every class counts as used.
    private static Object unsafe;
    private static Method shouldBeInitialized;

    /** Find the generators, initializing their classes but SecureRandom's:
     *  any state they take now is replaced by run() after a restore.  One
     *  first initialized after a restore would seed from the clock, which
     *  clones restored in the same millisecond share. */
    static void prepare() {
        for (String[] s : SEED_SOURCES)
            if (read(field(s[0], s[1])) instanceof AtomicLong gen)
                GENERATORS.add(gen);
        for (String[] s : SHARED_RANDOMS)
            if (read(field(s[0], s[1])) instanceof Random r)
                RANDOMS.add(r);
        collectionsRandom = field("java.util.Collections", "r");
        uuidRandom = field("java.util.UUID$Holder", "numberGenerator");
        drbgSeeder = field("sun.security.provider.AbstractDrbg$SeederHolder", "seeder");
        sha1Seeder = field("sun.security.provider.SecureRandom$SeederHolder", "seeder");
        try {
            Method m = Class.forName("sun.security.provider.SecureRandom", false, null)
                .getDeclaredMethod("engineSetSeed", byte[].class);
            m.setAccessible(true);
            sha1SetSeed = m;
        } catch (ReflectiveOperationException | RuntimeException e) {
            System.err.println("hl_java_dispatch: cannot reseed SHA1PRNG: " + e);
        }
        try {
            Method m = Class.forName("sun.security.provider.AbstractDrbg", false, null)
                .getDeclaredMethod("engineReseed", java.security.SecureRandomParameters.class);
            m.setAccessible(true);
            drbgReseed = m;
        } catch (ReflectiveOperationException | RuntimeException e) {
            System.err.println("hl_java_dispatch: cannot reseed DRBG: " + e);
        }
        for (String cls : new String[] {
                "sun.security.provider.NativePRNG",
                "sun.security.provider.NativePRNG$Blocking",
                "sun.security.provider.NativePRNG$NonBlocking" }) {
            Field f = field(cls, "INSTANCE");
            if (f != null)
                NATIVE_READERS.add(f);
        }
        nativeBuffered = field("sun.security.provider.NativePRNG$RandomIO", "buffered");
        try {
            Object u = Class.forName("jdk.internal.misc.Unsafe").getMethod("getUnsafe").invoke(null);
            Method m = u.getClass().getMethod("shouldBeInitialized", Class.class);
            m.invoke(u, Reseed.class);
            unsafe = u;
            shouldBeInitialized = m;
        } catch (ReflectiveOperationException | RuntimeException e) {
            System.err.println("hl_java_dispatch: cannot tell which generators were used: " + e);
        }
        try {
            Method init = Class.forName("java.util.concurrent.ThreadLocalRandom")
                .getDeclaredMethod("localInit");
            init.setAccessible(true);
            init.invoke(null);
            threadLocalInit = init;
        } catch (ReflectiveOperationException | RuntimeException e) {
            System.err.println("hl_java_dispatch: cannot reseed ThreadLocalRandom: " + e);
        }
    }

    static void run() {
        ByteBuffer seeds;
        try (var urandom = new FileInputStream("/dev/urandom")) {
            seeds = ByteBuffer.wrap(urandom.readNBytes(8 * (GENERATORS.size() + RANDOMS.size() + 1)));
        } catch (IOException e) {
            System.err.println("hl_java_dispatch: cannot reseed: " + e);
            return;
        }
        for (AtomicLong gen : GENERATORS)
            gen.set(seeds.getLong());
        for (Random r : RANDOMS)
            r.setSeed(seeds.getLong());
        if (read(collectionsRandom) instanceof Random r)
            r.setSeed(seeds.getLong());
        // The DRBG seeder first: a DRBG reseeds from it.  Each reseed also
        // takes fresh bytes as additional input, so it differs from every
        // other clone's even where its entropy source was cloned too.
        if (used(drbgSeeder) && drbgReseed != null) {
            try {
                Object seeder = read(drbgSeeder);
                for (Field f : seeder.getClass().getDeclaredFields()) {
                    f.setAccessible(true);
                    Object drbg = f.get(seeder);
                    if (drbgReseed.getDeclaringClass().isInstance(drbg))
                        drbgReseed.invoke(drbg, DrbgParameters.reseed(false, fresh()));
                }
            } catch (ReflectiveOperationException | RuntimeException e) {
                System.err.println("hl_java_dispatch: cannot reseed the DRBG seeder: " + e);
            }
        }
        // setSeed() adds to a SHA1PRNG's state, never replaces it.
        if (used(sha1Seeder) && sha1SetSeed != null) {
            try {
                sha1SetSeed.invoke(read(sha1Seeder), (Object) fresh());
            } catch (ReflectiveOperationException | RuntimeException e) {
                System.err.println("hl_java_dispatch: cannot reseed the SHA1PRNG seeder: " + e);
            }
        }
        try {
            if (used(uuidRandom) && read(uuidRandom) instanceof SecureRandom r
                    && r.getParameters() != null)
                r.reseed(DrbgParameters.reseed(false, fresh()));
        } catch (RuntimeException e) {
            System.err.println("hl_java_dispatch: cannot reseed UUID's SecureRandom: " + e);
        }
        for (Field f : NATIVE_READERS) {
            try {
                if (nativeBuffered != null && used(f) && read(f) instanceof Object io)
                    nativeBuffered.setInt(io, 0);
            } catch (ReflectiveOperationException | RuntimeException e) {
                System.err.println("hl_java_dispatch: cannot reseed NativePRNG: " + e);
            }
        }
        // ThreadLocalRandom's seed is each thread's own, taken from the
        // seeder when the thread first uses it: the thread each run call's
        // code gets (Snippets) takes it from the seeder reseeded above.
        // Guest function calls run on this thread, which the snapshot has:
        // seed it again from that seeder.
        if (threadLocalInit != null) {
            try {
                threadLocalInit.invoke(null);
            } catch (ReflectiveOperationException | RuntimeException e) {
                System.err.println("hl_java_dispatch: cannot reseed ThreadLocalRandom: " + e);
            }
        }
    }

    /** A static field, accessible, or null when the JDK has no such field:
     *  a generator a later JDK renamed or dropped is reported, not fatal. */
    private static Field field(String cls, String name) {
        try {
            // Not initialized here: f.get() does that, where it is read.
            Field f = Class.forName(cls, false, null).getDeclaredField(name);
            f.setAccessible(true);
            return f;
        } catch (ClassNotFoundException | NoSuchFieldException e) {
            System.err.println("hl_java_dispatch: " + cls + "." + name + " not found; not reseeded");
            return null;
        } catch (RuntimeException e) {
            System.err.println("hl_java_dispatch: cannot reseed " + cls + "." + name + ": " + e);
            return null;
        }
    }

    /** 32 bytes from the kernel's generator. */
    private static byte[] fresh() {
        try (var urandom = new FileInputStream("/dev/urandom")) {
            return urandom.readNBytes(32);
        } catch (IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
    }

    /** Whether f's class was initialized: if not, nothing the snapshot
     *  holds is drawn from it, and reading f would initialize it. */
    private static boolean used(Field f) {
        if (f == null)
            return false;
        if (shouldBeInitialized == null)
            return true;
        try {
            return !(boolean) shouldBeInitialized.invoke(unsafe, f.getDeclaringClass());
        } catch (ReflectiveOperationException | RuntimeException e) {
            return true;
        }
    }

    private static Object read(Field f) {
        try {
            return f == null ? null : f.get(null);
        } catch (ReflectiveOperationException e) {
            return null;
        }
    }
}
