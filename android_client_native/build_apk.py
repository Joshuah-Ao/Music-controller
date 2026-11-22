import os
import sys
import subprocess
import zipfile
import shutil

os.chdir(os.path.dirname(os.path.abspath(__file__)))

def find_tools_dir():
    candidates = [
        os.environ.get("ANDROID_BUILD_TOOLS", "").strip(),
        r"F:\Android SDK",
        r"..\..\android_build_tools",
    ]
    for d in candidates:
        if d and os.path.exists(os.path.join(d, "aapt2.exe")) and os.path.exists(os.path.join(d, "android.jar")):
            return d
    return r"F:\Android SDK"


TOOLS_DIR = find_tools_dir()
print(f"Using Android build tools dir: {TOOLS_DIR}")

AAPT2 = os.path.join(TOOLS_DIR, "aapt2.exe")
ANDROID_JAR = os.path.join(TOOLS_DIR, "android.jar")
R8_JAR = os.path.join(TOOLS_DIR, "r8.jar")
APKSIG_JAR = os.path.join(TOOLS_DIR, "apksig.jar")

BUILD_DIR = "build"
GEN_DIR = os.path.join(BUILD_DIR, "gen")
BIN_DIR = os.path.join(BUILD_DIR, "bin")

if os.path.exists(GEN_DIR):
    shutil.rmtree(GEN_DIR)
if os.path.exists(BIN_DIR):
    shutil.rmtree(BIN_DIR)
os.makedirs(GEN_DIR, exist_ok=True)
os.makedirs(BIN_DIR, exist_ok=True)

compiled_res = os.path.join(BUILD_DIR, "compiled_res.zip")
unaligned_apk = os.path.join(BUILD_DIR, "unaligned.apk")
unsigned_apk = os.path.join(BUILD_DIR, "unsigned.apk")
final_apk = os.path.join(BUILD_DIR, "music_controller_release.apk")
keystore = os.path.join(BUILD_DIR, "release.keystore")

print("=== [1/6] AAPT2 编译资源 ===")
cmd_compile = [AAPT2, "compile", "--dir", "res", "-o", compiled_res]
print("Running:", " ".join(cmd_compile))
subprocess.run(cmd_compile, check=True)

print("\n=== [2/6] AAPT2 链接资源并生成 R.java ===")
cmd_link = [
    AAPT2, "link",
    "-I", ANDROID_JAR,
    compiled_res,
    "--manifest", "AndroidManifest.xml",
    "-o", unaligned_apk,
    "--java", GEN_DIR,
    "--auto-add-overlay"
]
print("Running:", " ".join(cmd_link))
subprocess.run(cmd_link, check=True)

print("\n=== [3/6] JAVAC 编译全部 Java 源码 ===")
java_files = [os.path.join(GEN_DIR, "com", "naiveclient", "R.java")]
for root, dirs, files in os.walk("src"):
    for f in sorted(files):
        if f.endswith(".java"):
            java_files.append(os.path.join(root, f))

cmd_javac = [
    "javac",
    "-encoding", "UTF-8",
    "-cp", ANDROID_JAR,
    "-d", BIN_DIR,
    "--release", "8",
] + java_files
print("Running:", " ".join(cmd_javac))
subprocess.run(cmd_javac, check=True)

print("\n=== [4/6] D8 生成 classes.dex ===")
class_files = []
for root, dirs, files in os.walk(BIN_DIR):
    for f in files:
        if f.endswith(".class") and not f.startswith("Signer"):
            class_files.append(os.path.join(root, f))

cmd_d8 = [
    "java", "-cp", R8_JAR,
    "com.android.tools.r8.D8",
    "--lib", ANDROID_JAR,
    "--release",
    "--min-api", "21",
    "--output", BUILD_DIR
] + class_files
print("Running:", " ".join(cmd_d8))
subprocess.run(cmd_d8, check=True)

print("\n=== [5/6] 打包 classes.dex 到未签名 APK ===")
shutil.copy(unaligned_apk, unsigned_apk)
dex_file = os.path.join(BUILD_DIR, "classes.dex")
with zipfile.ZipFile(unsigned_apk, "a", compression=zipfile.ZIP_DEFLATED) as z:
    z.write(dex_file, "classes.dex")
print(f"Added classes.dex ({os.path.getsize(dex_file)} bytes) to {unsigned_apk}")

print("\n=== [6/6] 使用 ApkSigner 进行 V1 + V2 + V3 全标准签名 ===")
if not os.path.exists(keystore):
    cmd_genkey = [
        "keytool", "-genkeypair",
        "-keystore", keystore,
        "-storepass", "music123456",
        "-keypass", "music123456",
        "-alias", "music_release",
        "-keyalg", "RSA",
        "-keysize", "2048",
        "-validity", "10000",
        "-dname", "CN=MusicController, OU=App, O=Music, L=SZ, ST=GD, C=CN"
    ]
    print("Generating keystore...")
    subprocess.run(cmd_genkey, check=True)

signer_java_content = """
import com.android.apksig.ApkSigner;
import java.io.File;
import java.io.FileInputStream;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.cert.X509Certificate;
import java.util.Collections;
import java.util.List;

public class Signer {
    public static void main(String[] args) throws Exception {
        File keystoreFile = new File(args[0]);
        String storePass = args[1];
        String alias = args[2];
        File inApk = new File(args[3]);
        File outApk = new File(args[4]);

        KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
        try (FileInputStream fis = new FileInputStream(keystoreFile)) {
            ks.load(fis, storePass.toCharArray());
        }

        PrivateKey privateKey = (PrivateKey) ks.getKey(alias, storePass.toCharArray());
        X509Certificate cert = (X509Certificate) ks.getCertificate(alias);
        List<X509Certificate> certs = Collections.singletonList(cert);

        ApkSigner.SignerConfig signerConfig = new ApkSigner.SignerConfig.Builder("CERT", privateKey, certs).build();

        ApkSigner apkSigner = new ApkSigner.Builder(Collections.singletonList(signerConfig))
                .setInputApk(inApk)
                .setOutputApk(outApk)
                .setV1SigningEnabled(true)
                .setV2SigningEnabled(true)
                .setV3SigningEnabled(true)
                .build();

        apkSigner.sign();
        System.out.println("ApkSigner finished successfully: v1, v2, v3 enabled!");
    }
}
"""
signer_java_file = os.path.join(BUILD_DIR, "Signer.java")
with open(signer_java_file, "w", encoding="utf-8") as f:
    f.write(signer_java_content)

subprocess.run(["javac", "-cp", APKSIG_JAR, "-d", BUILD_DIR, signer_java_file], check=True)

if os.path.exists(final_apk):
    os.remove(final_apk)

subprocess.run([
    "java", "-cp", f"{BUILD_DIR};{APKSIG_JAR}",
    "Signer",
    keystore, "music123456", "music_release",
    unsigned_apk, final_apk
], check=True)

print(f"\n[SUCCESS] 打包完成！最终 APK 大小: {os.path.getsize(final_apk)} 字节，路径: {final_apk}")
