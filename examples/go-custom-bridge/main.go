// Command go-custom-bridge is a Constellation bridge: write workflow results into your system in apply, and
// carry out approved actions in act. Check-in, cursor, paging and reporting are results.Bridge.
package main

import (
	_ "embed"
	"fmt"
	"os"

	"github.com/constellationhq/connectors/cli/results"
)

//go:embed connector.json
var manifest []byte

// apply writes one run's items into your system, keyed on item["entity"]["id"], and returns
// what it wrote. The run is the whole answer for its window: withdraw what you wrote earlier
// for run.Workflow inside the window that items no longer carries.
func apply(items []results.Item, settings map[string]any, run results.Run) (map[string]int, error) {
	min, _ := settings["min_p10_gb"].(float64)
	passes := 0
	for _, item := range items {
		value, _ := item["value"].(map[string]any)
		if p10, ok := value["p10"].(float64); ok && p10 < min {
			continue
		}
		entity, _ := item["entity"].(map[string]any)
		fmt.Printf("%v: %v at %v\n", settings["label"], entity["id"], item["target_at"])
		passes++
	}
	return map[string]int{"passes": passes}, nil
}

// act carries out an approved action (book the backup pass, cancel the original) and returns
// true once done. Only approved actions arrive here; an error retries the run.
func act(item results.Item, settings map[string]any, run results.Run) (bool, error) {
	action := item["action"].(map[string]any)
	fmt.Printf("approved: %v %v -> %v\n", action["verb"], action["from"], action["to"])
	return true, nil
}

func main() {
	bridge, err := results.NewBridge(manifest)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	bridge.Apply, bridge.Act = apply, act
	if err := bridge.RunForever(); err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v. Stopped; start it again once that is done\n", bridge.Consumer(), err)
		os.Exit(2)
	}
}
