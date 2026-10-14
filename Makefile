# cc-sessions. `make install` copies into $(PREFIX); nothing is loaded or started.
PREFIX        ?= $(HOME)/.claude
LAUNCHAGENTS  ?= $(HOME)/Library/LaunchAgents
LOG_DIR       ?= $(HOME)/Library/Logs
DAEMON_PYTHON ?= $(HOME)/.claude/venvs/iterm2/bin/python
PYTHON        ?= /usr/bin/python3

BIN_FILES  = cc-sessions cc-resume
LIB_FILES  = $(notdir $(wildcard lib/ccsessions/*.py))
LIBDIR     = $(PREFIX)/lib/cc-sessions
PLIST      = com.cc-sessions.daemon.plist

.PHONY: install uninstall check test release-check render-plist

install: render-plist
	mkdir -p $(PREFIX)/bin $(LIBDIR)/ccsessions $(LAUNCHAGENTS)
	for f in $(BIN_FILES); do install -m 755 bin/$$f $(PREFIX)/bin/$$f; done
	for f in $(LIB_FILES); do install -m 644 lib/ccsessions/$$f $(LIBDIR)/ccsessions/$$f; done
	install -m 644 daemon/cc_sessions_daemon.py $(LIBDIR)/cc_sessions_daemon.py
	install -m 644 build/$(PLIST) $(LAUNCHAGENTS)/$(PLIST)
	@echo "installed. load the daemon with:"
	@echo "  launchctl bootstrap gui/$$(id -u) $(LAUNCHAGENTS)/$(PLIST)"
	@echo "or restart a running one (it keeps the old code until then):"
	@echo "  launchctl kickstart -k gui/$$(id -u)/com.cc-sessions.daemon"

render-plist:
	mkdir -p build
	sed -e 's|@DAEMON_PYTHON@|$(DAEMON_PYTHON)|g' \
	    -e 's|@DAEMON_SCRIPT@|$(LIBDIR)/cc_sessions_daemon.py|g' \
	    -e 's|@LOG_DIR@|$(LOG_DIR)|g' launchd/$(PLIST).in > build/$(PLIST)

uninstall:
	for f in $(BIN_FILES); do rm -f $(PREFIX)/bin/$$f; done
	rm -rf $(LIBDIR)
	rm -f $(LAUNCHAGENTS)/$(PLIST)

# exit 1 when any installed file differs from the repo
check: render-plist
	@rc=0; \
	for f in $(BIN_FILES); do cmp -s bin/$$f $(PREFIX)/bin/$$f || { echo "differs: $(PREFIX)/bin/$$f"; rc=1; }; done; \
	for f in $(LIB_FILES); do cmp -s lib/ccsessions/$$f $(LIBDIR)/ccsessions/$$f || { echo "differs: $(LIBDIR)/ccsessions/$$f"; rc=1; }; done; \
	cmp -s daemon/cc_sessions_daemon.py $(LIBDIR)/cc_sessions_daemon.py || { echo "differs: $(LIBDIR)/cc_sessions_daemon.py"; rc=1; }; \
	cmp -s build/$(PLIST) $(LAUNCHAGENTS)/$(PLIST) || { echo "differs: $(LAUNCHAGENTS)/$(PLIST)"; rc=1; }; \
	[ $$rc -eq 0 ] && echo "installed copies match the repo"; exit $$rc

test:
	$(PYTHON) -m unittest discover -s tests

# fails when the tree contains any string listed (one per line) in the file named by
# $CC_SESSIONS_PRIVATE_STRINGS; skipped when the variable is unset. LICENSE is exempt (it
# carries the copyright holder's name on purpose).
release-check:
	@if [ -z "$$CC_SESSIONS_PRIVATE_STRINGS" ]; then echo "release-check: CC_SESSIONS_PRIVATE_STRINGS unset, skipped"; exit 0; fi; \
	mkdir -p build; grep -v '^[[:space:]]*$$' "$$CC_SESSIONS_PRIVATE_STRINGS" > build/.deny || true; \
	if [ ! -s build/.deny ]; then echo "release-check: deny-list is empty"; exit 1; fi; \
	if grep -rnIiF -f build/.deny --exclude-dir=.git --exclude-dir=build --exclude-dir=__pycache__ --exclude=LICENSE . ; then \
	  echo "release-check: private strings found (above)"; exit 1; \
	else echo "release-check: clean"; fi
