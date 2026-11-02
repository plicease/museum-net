# A small archive for the lab. The binary holds every byte value, so a transfer
# that translates line endings or strips the high bit changes its checksum.
{ runCommand, python3 }:
runCommand "museum-archive-fixture" { nativeBuildInputs = [ python3 ]; } ''
  cp -r --no-preserve=mode ${../fixtures/archive} $out
  python3 -c '
  import sys
  block = bytes(range(256))
  with open(sys.argv[1], "wb") as out:
      for _ in range(8192):
          out.write(block)
  ' $out/PUB/DOS/BINARY.BIN
''
