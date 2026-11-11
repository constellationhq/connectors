package templates

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/constellationhq/connectors/cli/conformance"
)

func cliDir(t *testing.T) string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Dir(filepath.Dir(file))
}

func sh(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%s: %v\n%s", strings.Join(args, " "), err, out)
	}
}

func conforms(t *testing.T, dir string, command ...string) {
	t.Helper()
	var out strings.Builder
	result, err := conformance.Run(conformance.Options{Command: command, Timeout: 60 * time.Second, Output: &out})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range result.Checks {
		if !c.OK {
			t.Errorf("FAIL %s: %s", c.Name, c.Detail)
		}
	}
	if !result.Passed {
		t.Logf("bridge output:\n%s", out.String())
	}
}

func TestScaffoldRefusesABadNameLanguageOrFolder(t *testing.T) {
	dir := t.TempDir()
	if err := Scaffold(filepath.Join(dir, "a"), "Bad Name", "go", "dev"); err == nil {
		t.Error("a bad name was taken")
	}
	if err := Scaffold(filepath.Join(dir, "b"), "ok", "cobol", "dev"); err == nil || !strings.Contains(err.Error(), "python, ts, go") {
		t.Errorf("a bad language: %v", err)
	}
	if err := Scaffold(dir, "ok", "go", "dev"); err == nil {
		t.Error("an existing folder was written into")
	}
}

func TestEveryScaffoldHasABridgeATestAReadmeAndADockerfile(t *testing.T) {
	want := map[string][]string{
		"python": {"bridge.py", "test_bridge.py", "README.md", "Dockerfile", "connector.json", "requirements.txt"},
		"ts":     {"bridge.ts", "bridge.test.ts", "README.md", "Dockerfile", "connector.json", "package.json"},
		"go":     {"main.go", "main_test.go", "README.md", "Dockerfile", "connector.json", "go.mod"},
	}
	for _, lang := range Langs {
		dir := filepath.Join(t.TempDir(), "x")
		if err := Scaffold(dir, "my-mcs-bridge", lang, "v1.2.3"); err != nil {
			t.Fatal(err)
		}
		for _, f := range want[lang] {
			raw, err := os.ReadFile(filepath.Join(dir, f))
			if err != nil {
				t.Errorf("%s: %v", lang, err)
			}
			if strings.Contains(string(raw), "{{") {
				t.Errorf("%s/%s has an unfilled placeholder", lang, f)
			}
		}
	}
}

// The bridge's user must be able to write the volume its state lives in: a bridge that cannot
// keep its cursor and the approvals it carried out repeats them after every restart.
func TestEveryDockerfileKeepsStateInAVolumeItsUserCanWrite(t *testing.T) {
	owner := map[string]string{"python": "chown nobody ", "ts": "chown node ", "go": "--chown=65532:65532 /state "}
	for _, lang := range Langs {
		dir := filepath.Join(t.TempDir(), "x")
		if err := Scaffold(dir, "my-mcs-bridge", lang, "v1.2.3"); err != nil {
			t.Fatal(err)
		}
		raw, _ := os.ReadFile(filepath.Join(dir, "Dockerfile"))
		text := string(raw)
		if !strings.Contains(text, "ENV CURSOR_FILE=/var/lib/constellation/cursor.txt\n") ||
			!strings.Contains(text, "VOLUME /var/lib/constellation\n") ||
			!strings.Contains(text, owner[lang]+"/var/lib/constellation") {
			t.Errorf("%s: the state volume is not the user's:\n%s", lang, text)
		}
		if lang == "go" && !strings.Contains(text, ":nonroot\n") {
			t.Errorf("go: not the distroless nonroot (65532) image:\n%s", text)
		}
	}
}

func TestTheGoScaffoldBuildsTestsAndConforms(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "my-mcs-bridge")
	if err := Scaffold(dir, "my-mcs-bridge", "go", "dev"); err != nil {
		t.Fatal(err)
	}
	sh(t, dir, "go", "mod", "edit", "-replace", Module+"="+cliDir(t))
	sh(t, dir, "go", "test", "./...")
	sh(t, dir, "go", "build", "-o", "bridge", ".")
	conforms(t, dir, filepath.Join(dir, "bridge"))
}

func TestThePythonScaffoldConforms(t *testing.T) {
	if exec.Command("python3", "-c", "import constellation_space.bridges.template").Run() != nil {
		t.Skip("the constellation-space package is not installed")
	}
	dir := filepath.Join(t.TempDir(), "my-mcs-bridge")
	if err := Scaffold(dir, "my-mcs-bridge", "python", "dev"); err != nil {
		t.Fatal(err)
	}
	if exec.Command("python3", "-c", "import pytest").Run() == nil {
		sh(t, dir, "python3", "-m", "pytest", "-q")
	}
	conforms(t, dir, "python3", filepath.Join(dir, "bridge.py"))
}

// The TypeScript scaffold needs @constellationspace/client, which is not on a public registry:
// CONSTELLATION_TS_CLIENT names a built clients/typescript folder to install it from.
func TestTheTypeScriptScaffoldConforms(t *testing.T) {
	client := os.Getenv("CONSTELLATION_TS_CLIENT")
	if client == "" {
		t.Skip("set CONSTELLATION_TS_CLIENT to a built clients/typescript to run it")
	}
	dir := filepath.Join(t.TempDir(), "my-mcs-bridge")
	if err := Scaffold(dir, "my-mcs-bridge", "ts", "dev"); err != nil {
		t.Fatal(err)
	}
	sh(t, dir, "npm", "install", "--no-audit", "--no-fund", client)
	sh(t, dir, "npm", "test")
	conforms(t, dir, "node", "--experimental-strip-types", filepath.Join(dir, "bridge.ts"))
}

func TestAReleasedScaffoldInstallsTheClientByHashNeverByNameAlone(t *testing.T) {
	PythonHashes, NPMIntegrity = "sha256:"+strings.Repeat("a", 64)+",sha256:"+strings.Repeat("b", 64), "sha512-Zm9v"
	defer func() { PythonHashes, NPMIntegrity = "", "" }()
	py := filepath.Join(t.TempDir(), "py")
	if err := Scaffold(py, "my-mcs-bridge", "python", "v1.2.3"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(py, "requirements.txt"))
	want := "constellation-space==1.2.3 \\\n    --hash=sha256:" + strings.Repeat("a", 64) + " \\\n    --hash=sha256:" + strings.Repeat("b", 64) + "\n"
	if !strings.HasSuffix(string(raw), want) {
		t.Errorf("requirements.txt:\n%s", raw)
	}
	ts := filepath.Join(t.TempDir(), "ts")
	if err := Scaffold(ts, "my-mcs-bridge", "ts", "v1.2.3"); err != nil {
		t.Fatal(err)
	}
	var lock struct {
		Packages map[string]struct{ Version, Resolved, Integrity string } `json:"packages"`
	}
	raw, _ = os.ReadFile(filepath.Join(ts, "package-lock.json"))
	if err := json.Unmarshal(raw, &lock); err != nil {
		t.Fatalf("package-lock.json: %v\n%s", err, raw)
	}
	client := lock.Packages["node_modules/@constellationspace/client"]
	if client.Version != "1.2.3" || client.Integrity != "sha512-Zm9v" ||
		client.Resolved != "https://registry.npmjs.org/@constellationspace/client/-/client-1.2.3.tgz" {
		t.Errorf("the client is not locked: %+v", client)
	}
	raw, _ = os.ReadFile(filepath.Join(ts, "package.json"))
	if !strings.Contains(string(raw), `"@constellationspace/client": "1.2.3"`) {
		t.Errorf("package.json does not pin the exact version:\n%s", raw)
	}
}

func TestAScaffoldFromAnUnreleasedCLINamesNoPackageItCannotPin(t *testing.T) {
	py := filepath.Join(t.TempDir(), "py")
	if err := Scaffold(py, "my-mcs-bridge", "python", "v1.2.3"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(py, "requirements.txt"))
	for _, line := range strings.Split(string(raw), "\n") {
		if line != "" && !strings.HasPrefix(line, "#") {
			t.Errorf("requirements.txt asks for %q with no hash", line)
		}
	}
	ts := filepath.Join(t.TempDir(), "ts")
	if err := Scaffold(ts, "my-mcs-bridge", "ts", "v1.2.3"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(ts, "package-lock.json")); !os.IsNotExist(err) {
		t.Errorf("a lockfile with no integrity was written: %v", err)
	}
}
