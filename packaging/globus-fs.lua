-- Lmod modulefile for a standalone globus-fs / globus-console install.
--
-- Install as e.g. /sw/modules/globus-fs/0.2.0.lua and set `root` to the
-- PREFIX given to packaging/build-standalone.sh. The install carries its
-- own Python, so this module loads nothing else and conflicts with nothing.

local root = "/sw/pkgs/arc/globus-fs/0.2.0"

whatis("Name: globus-fs")
whatis("Version: 0.2.0")
whatis("Description: ls, find, du and archivescan for Globus collections; " ..
       "globus-console for the Activity view")

help([[
globus-fs       ls, find, du, archivescan, bookmarks and your own transfers
globus-console  the Globus console Activity tab (needs an activity role)

Run `globus-fs login` once, then e.g. `globus-fs du dataden:/mylab -d 1`.
]])

-- Only the two commands: the bundled python is deliberately not on PATH.
prepend_path("PATH", pathJoin(root, "bin"))

-- Site collection shortcuts (dataden:, turbo:, ...). Optional if you ship
-- /etc/globus-fs/collections.conf instead.
setenv("GLOBUS_FS_COLLECTIONS", pathJoin(root, "etc/collections.conf"))

-- Uncomment where a tier migrates to tape at a different size.
-- setenv("GLOBUS_FS_ARCHIVESCAN_SIZE", "100M")
