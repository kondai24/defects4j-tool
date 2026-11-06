// Rewrites compiled classes for the verify step (JDK 11+, no external jars).
//
// Usage:
//   java --add-exports java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED \
//        --add-exports java.base/jdk.internal.org.objectweb.asm.commons=ALL-UNNAMED \
//        ClassRewriter.java <classes_dir> <out_dir> <prefix|-> <spec_file>
//
// spec_file lines:
//   rename <internal/Name>   copy the class to <out_dir>/<prefix>/<internal/Name>; every reference
//                            to a renamed class (in any rewritten class) points to the new name.
//   copy <internal/Name>     copy the class without renaming (used to publicize a class only).
//   public <internal/Name>   make the class (and its InnerClasses entries) public.
//   public-method <internal/Name> <name> <descriptor>
//                            make the method public so the harness can call it from another package.
// Names and descriptors in the spec are always the original (not renamed) ones.
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import jdk.internal.org.objectweb.asm.ClassReader;
import jdk.internal.org.objectweb.asm.ClassVisitor;
import jdk.internal.org.objectweb.asm.ClassWriter;
import jdk.internal.org.objectweb.asm.MethodVisitor;
import jdk.internal.org.objectweb.asm.Opcodes;
import jdk.internal.org.objectweb.asm.commons.ClassRemapper;
import jdk.internal.org.objectweb.asm.commons.SimpleRemapper;

public class ClassRewriter {
    private static final int API = Opcodes.ASM5;

    private static int makePublic(int access) {
        return (access & ~(Opcodes.ACC_PRIVATE | Opcodes.ACC_PROTECTED)) | Opcodes.ACC_PUBLIC;
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 4) {
            System.err.println("usage: ClassRewriter <classes_dir> <out_dir> <prefix|-> <spec_file>");
            System.exit(2);
        }
        Path classes = Paths.get(args[0]);
        Path out = Paths.get(args[1]);
        String prefix = args[2].equals("-") ? "" : args[2];
        List<String> spec = Files.readAllLines(Paths.get(args[3]));

        Set<String> rename = new HashSet<>();
        Set<String> copy = new HashSet<>();
        Set<String> publicClasses = new HashSet<>();
        Set<String> publicMethods = new HashSet<>();
        for (String line : spec) {
            String[] f = line.trim().split("\\s+");
            if (f.length == 0 || f[0].isEmpty()) continue;
            switch (f[0]) {
                case "rename": rename.add(f[1]); break;
                case "copy": copy.add(f[1]); break;
                case "public": publicClasses.add(f[1]); break;
                case "public-method": publicMethods.add(f[1] + "." + f[2] + f[3]); break;
                default: throw new IllegalArgumentException("unknown spec line: " + line);
            }
        }
        if (!rename.isEmpty() && prefix.isEmpty()) {
            throw new IllegalArgumentException("rename requires a prefix");
        }
        Map<String, String> mapping = new HashMap<>();
        Map<String, String> reverse = new HashMap<>();
        for (String name : rename) {
            mapping.put(name, prefix + "/" + name);
            reverse.put(prefix + "/" + name, name);
        }
        SimpleRemapper remapper = new SimpleRemapper(mapping);
        SimpleRemapper unmapper = new SimpleRemapper(reverse);

        Set<String> all = new HashSet<>(rename);
        all.addAll(copy);
        for (String name : all) {
            ClassReader reader = new ClassReader(Files.readAllBytes(classes.resolve(name + ".class")));
            ClassWriter writer = new ClassWriter(0);
            // The publicizer sees already-renamed names, so map them back before matching the spec.
            ClassVisitor publicizer = new ClassVisitor(API, writer) {
                private String owner;

                @Override
                public void visit(int version, int access, String n, String sig, String sup, String[] itf) {
                    owner = unmapper.map(n) == null ? n : unmapper.map(n);
                    int acc = publicClasses.contains(owner) ? makePublic(access) : access;
                    super.visit(version, acc, n, sig, sup, itf);
                }

                @Override
                public void visitInnerClass(String n, String outer, String inner, int access) {
                    String orig = unmapper.map(n) == null ? n : unmapper.map(n);
                    int acc = publicClasses.contains(orig) ? makePublic(access) : access;
                    super.visitInnerClass(n, outer, inner, acc);
                }

                @Override
                public MethodVisitor visitMethod(int access, String n, String desc, String sig, String[] exc) {
                    String key = owner + "." + n + unmapper.mapMethodDesc(desc);
                    int acc = publicMethods.contains(key) ? makePublic(access) : access;
                    return super.visitMethod(acc, n, desc, sig, exc);
                }
            };
            ClassVisitor chain = rename.isEmpty() ? publicizer : new ClassRemapper(publicizer, remapper);
            reader.accept(chain, 0);
            String outName = rename.contains(name) ? prefix + "/" + name : name;
            Path dest = out.resolve(outName + ".class");
            Files.createDirectories(dest.getParent());
            Files.write(dest, writer.toByteArray());
        }
    }
}
