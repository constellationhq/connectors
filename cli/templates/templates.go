// Package templates scaffolds a working bridge in Python, TypeScript or Go.
package templates

import (
	"embed"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"strings"
	"text/template"

	"github.com/constellationhq/connectors/cli/results"
)

//go:embed connector.json.tmpl python ts go
var files embed.FS

// Langs are the languages a bridge can be scaffolded in.
var Langs = []string{"python", "ts", "go"}

// Module is this CLI's Go module path, which a Go bridge requires for its results package.
var Module = path.Dir(reflect.TypeOf(results.Bridge{}).PkgPath())

// The hashes of the client packages released with this CLI, stamped by the release build
// (-ldflags -X), so a scaffold installs exactly those files whichever index serves the name:
// PythonHashes is the wheel's and sdist's "sha256:<hex>", comma separated; NPMIntegrity the npm
// tarball's "sha512-<base64>". Empty in any other build: then the scaffold names no package.
var PythonHashes, NPMIntegrity string

// Scaffold writes a bridge named `name` in `lang` into the new folder `dir`. `version` is the
// CLI's version: a Go bridge requires the results package at it, a TypeScript one the client, since
// one release ships all three.
func Scaffold(dir, name, lang, version string) error {
	if !results.NamePattern.MatchString(name) {
		return fmt.Errorf("%q is not a bridge name: a lowercase slug (a-z, 0-9, -), at most 63 characters", name)
	}
	if _, err := fs.Stat(files, lang); err != nil || lang == "connector.json.tmpl" {
		return fmt.Errorf("--lang is one of %s", strings.Join(Langs, ", "))
	}
	if _, err := os.Stat(dir); err == nil {
		return fmt.Errorf("%s already exists", dir)
	}
	if !strings.HasPrefix(version, "v") {
		version = "v0.0.0"
	}
	words := strings.Split(name, "-")
	for i, w := range words {
		words[i] = strings.ToUpper(w[:1]) + w[1:]
	}
	class := strings.Join(words, "")
	if class[0] >= '0' && class[0] <= '9' {
		class = "Bridge" + class
	}
	var pythonHashes []string
	if PythonHashes != "" {
		pythonHashes = strings.Split(PythonHashes, ",")
	}
	data := map[string]any{"Name": name, "Title": strings.Join(words, " "), "Class": class,
		"Module": Module, "Version": version, "SemVer": strings.TrimPrefix(version, "v"),
		"PythonHashes": pythonHashes, "NPMIntegrity": NPMIntegrity}
	paths := []string{"connector.json.tmpl"}
	entries, _ := fs.ReadDir(files, lang)
	for _, e := range entries {
		paths = append(paths, lang+"/"+e.Name())
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	for _, p := range paths {
		t, err := template.ParseFS(files, p)
		if err != nil {
			return err
		}
		var text strings.Builder
		if err := t.Execute(&text, data); err != nil {
			return err
		}
		// A template that renders nothing (the lockfile, with no hash to lock) is not written.
		if strings.TrimSpace(text.String()) == "" {
			continue
		}
		if err := os.WriteFile(filepath.Join(dir, strings.TrimSuffix(path.Base(p), ".tmpl")), []byte(text.String()), 0o644); err != nil {
			return err
		}
	}
	return nil
}
