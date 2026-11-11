package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Every release is signed keyless with Sigstore by the release workflow of the public
// constellationhq/connectors repository, run for a vX.Y.Z tag: cosign checks the signing
// certificate names that workflow and GitHub's OIDC issuer, and that the signature is in
// the public transparency log.
const (
	releaseIdentity = `^https://github\.com/constellationhq/connectors/\.github/workflows/release\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`
	releaseIssuer   = "https://token.actions.githubusercontent.com"
)

const verifyUsage = `usage: constellation verify DIR

Checks a downloaded release: that DIR's SHA256SUMS is signed by the constellationhq/connectors
release workflow (with cosign, which must be installed), then that every file it lists that is in
DIR matches it. Files not downloaded are named and skipped. Exits 1 when anything does not check.
`

func verify(args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprint(stderr, verifyUsage) }
	rest, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(rest) != 1 {
		fmt.Fprint(stderr, verifyUsage)
		return errUsage
	}
	dir := rest[0]
	sums, err := os.ReadFile(filepath.Join(dir, "SHA256SUMS"))
	if err != nil {
		return err
	}
	bundle := filepath.Join(dir, "SHA256SUMS.sigstore.json")
	if _, err := os.Stat(bundle); errors.Is(err, os.ErrNotExist) {
		return errors.New("SHA256SUMS is not signed (there is no SHA256SUMS.sigstore.json beside it):" +
			" nothing is verified. Download the signature with the release (the release is not signed" +
			" if it has none)")
	}
	cosign := os.Getenv("COSIGN")
	if cosign == "" {
		cosign = "cosign"
	}
	if _, err := exec.LookPath(cosign); err != nil {
		return errors.New("verify checks the release signature with cosign, which is not installed" +
			" (https://docs.sigstore.dev/cosign/system_config/installation/): nothing is verified")
	}
	out, err := exec.Command(cosign, "verify-blob", "--bundle", bundle,
		"--certificate-identity-regexp", releaseIdentity, "--certificate-oidc-issuer", releaseIssuer,
		filepath.Join(dir, "SHA256SUMS")).CombinedOutput()
	if err != nil {
		return fmt.Errorf("SHA256SUMS is not signed by the constellationhq/connectors release workflow: %s",
			strings.TrimSpace(string(out)))
	}
	fmt.Fprintln(stdout, "ok  SHA256SUMS is signed by the constellationhq/connectors release workflow")
	return verifySums(dir, sums, stdout)
}

// verifySums checks every file SHA256SUMS lists that is in `dir`; a listed file not downloaded
// is said so, and at least one must be there.
func verifySums(dir string, sums []byte, stdout io.Writer) error {
	checked := 0
	var bad []string
	scanner := bufio.NewScanner(bytes.NewReader(sums))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		want, name, ok := strings.Cut(line, " ")
		name = strings.TrimPrefix(strings.TrimSpace(name), "*")
		if !ok || len(want) != 64 || !filepath.IsLocal(name) {
			return fmt.Errorf("SHA256SUMS has a line that is not <sha256>  <file>: %q", line)
		}
		f, err := os.Open(filepath.Join(dir, name))
		if errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(stdout, "--  %s (not downloaded)\n", name)
			continue
		}
		if err != nil {
			return err
		}
		h := sha256.New()
		_, err = io.Copy(h, f)
		f.Close()
		if err != nil {
			return err
		}
		checked++
		if got := hex.EncodeToString(h.Sum(nil)); !strings.EqualFold(got, want) {
			bad = append(bad, name)
			fmt.Fprintf(stdout, "BAD %s (sha256 %s, SHA256SUMS says %s)\n", name, got, want)
			continue
		}
		fmt.Fprintf(stdout, "ok  %s\n", name)
	}
	if len(bad) > 0 {
		return fmt.Errorf("%d file(s) do not match SHA256SUMS: %s", len(bad), strings.Join(bad, ", "))
	}
	if checked == 0 {
		return errors.New("none of the files SHA256SUMS lists is in the folder")
	}
	return nil
}
