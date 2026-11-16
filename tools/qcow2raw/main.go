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

	"github.com/lima-vm/go-qcow2reader"
	"github.com/lima-vm/go-qcow2reader/convert"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "qcow2raw:", err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 3 || len(os.Args) > 4 {
		return fmt.Errorf("usage: qcow2raw <in.qcow2> <out.raw> [size-bytes]")
	}
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
