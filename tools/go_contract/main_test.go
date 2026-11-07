package main

import "testing"

func TestUnicodeBoundary(t *testing.T) {
	for _, raw := range []string{`"\ud800"`, `"\udfff"`, `"\ud800x"`, `"\ud800\u0041"`} {
		if executeRaw("digest", []byte(raw)).OK {
			t.Fatalf("accepted unpaired surrogate %s", raw)
		}
	}
	for _, raw := range []string{`"\ud83d\ude00"`, `"\\ud800"`, `"中文😀"`} {
		if !executeRaw("digest", []byte(raw)).OK {
			t.Fatalf("rejected valid Unicode %s", raw)
		}
	}
}

func TestLargeIntegerRemainsInteger(t *testing.T) {
	value := parseValue([]byte(`{"exit_code":0,"workspace_revision":1208925819614629174706176,"workspace_fingerprint":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","command":"test"}`))
	if verification(value)["succeeded"] != true {
		t.Fatal("unbounded legacy revision lost")
	}
	if canonical(parseValue([]byte(`-0`))) != "0" {
		t.Fatal("JSON integer zero normalization")
	}
}
