// qcow2raw converts a qcow2 disk image (Ubuntu's cloud image) into a sparse
// raw disk image that Lume / Virtualization.framework can boot, optionally
// growing the result to a larger virtual size.
//
//	qcow2raw <in.qcow2> <out.raw> [size-bytes]
//
// It uses github.com/lima-vm/go-qcow2reader, the same reader Lima uses to
// prepare cloud images for its vz driver, so no QEMU install is needed.
package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/lima-vm/go-qcow2reader"
	"github.com/lima-vm/go-qcow2reader/convert"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "qcow2raw:", err)
		os.Exit(1)
	}
}

const usage = "usage: qcow2raw <in.qcow2> <out.raw> [size-bytes]"

// metacomputer's command-line rules (scripts/strictargs.sh, lib/strictargs.py): exactly -h or --help prints the
// usage; an option or anything shaped like one (a path that starts with '-' is ./-name), a size that isn't a number,
// or a word too many or missing is a usage error (exit 2), before any file is opened or made.
func checkArgs(args []string) {
	if len(args) == 1 && (args[0] == "-h" || args[0] == "--help") {
		fmt.Println(usage)
		os.Exit(0)
	}
	bad := len(args) < 2 || len(args) > 3
	for i, a := range args {
		if strings.HasPrefix(a, "-") || (i == 2 && strings.Trim(a, "0123456789") != "") {
			bad = true
		}
	}
	if bad {
		fmt.Fprintln(os.Stderr, "qcow2raw: "+usage+" (nothing was done)")
		os.Exit(2)
	}
	if os.Getenv("MCOMP_PARSE_ONLY") == "1" { // tests/test_help.py's valid forms: taken, nothing done
		fmt.Printf("parse-only: qcow2raw: taken {\"flags\": [], \"values\": [], \"words\": %d, \"max_words\": 3}\n", len(args))
		os.Exit(0)
	}
}

func run() error {
	checkArgs(os.Args[1:])
	in, err := os.Open(os.Args[1])
	if err != nil {
		return err
	}
	defer in.Close()
	img, err := qcow2reader.Open(in)
	if err != nil {
		return err
	}
	defer img.Close()
	if err := img.Readable(); err != nil {
		return err
	}
	if t := img.Type(); t != "qcow2" {
		return fmt.Errorf("%s: expected qcow2, got %s", os.Args[1], t)
	}
	size := img.Size()
	if len(os.Args) == 4 {
		want, err := strconv.ParseInt(os.Args[3], 10, 64)
		if err != nil {
			return err
		}
		if want < size {
			return fmt.Errorf("requested size %d is smaller than the image (%d)", want, size)
		}
		size = want
	}
	out, err := os.OpenFile(os.Args[2], os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	ok := false
	defer func() {
		out.Close()
		if !ok {
			os.Remove(os.Args[2])
		}
	}()
	// Sparse: Convert skips zero runs, so set the final size first.
	if err := out.Truncate(size); err != nil {
		return err
	}
	if err := convert.Convert(out, img, convert.Options{}); err != nil {
		return err
	}
	if err := out.Sync(); err != nil {
		return err
	}
	ok = true
	fmt.Printf("%s -> %s (%d bytes virtual, image %d)\n", os.Args[1], os.Args[2], size, img.Size())
	return nil
}
