#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cc -Wall -Wextra -Werror ConfigurationArchive/ArchiveChannel.c ConfigurationArchive/Tests/ChannelTests.c -o "$work/channel-tests"
"$work/channel-tests"
if [ "$(uname -s)" != Darwin ]; then
    echo 'SKIP native Swift archive fixtures: macOS Foundation/CryptoKit toolchain required'
    exit 0
fi
xcrun clang -Wall -Wextra -Werror -c ConfigurationArchive/ArchiveChannel.c -o "$work/channel.o"
xcrun swiftc -parse-as-library -import-objc-header ConfigurationArchive/ArchiveChannel.h \
 ConfigurationArchive/ConfigurationSnapshot.swift ConfigurationArchive/ConfigurationDirectory.swift \
 ConfigurationArchive/ConfigurationTree.swift ConfigurationArchive/ConfigurationBackupEngine.swift \
 ConfigurationArchive/ConfigurationBackupStorage.swift ConfigurationArchive/ArchiveWire.swift \
 ConfigurationArchive/ArchiveService.swift ConfigurationArchive/Tests/IrisinStorageReference.swift \
 ConfigurationArchive/Tests/ArchiveFixtures.swift "$work/channel.o" -o "$work/archive-fixtures"
"$work/archive-fixtures"
