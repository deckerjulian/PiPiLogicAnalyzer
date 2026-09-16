/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of LogicAnalyzer 7, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __LOGICANALYZER_SELFTEST__
#define __LOGICANALYZER_SELFTEST__

//Board self-test (command 7). No signal may be connected to the board: the channel inputs are
//only exercised with the internal pull resistors, nothing is driven on them.
//
//Every result is reported as a line "SELFTEST:<item>:<status>:<detail>\n", the test ends with
//"SELFTEST_END\n". Status: OK, FAIL, INFO, SKIPPED, STUCK_HIGH, STUCK_LOW, INVERTED.

typedef void (*SELFTEST_REPORT)(const char* line, void* context);

void RunSelfTest(SELFTEST_REPORT report, void* context);

#endif
