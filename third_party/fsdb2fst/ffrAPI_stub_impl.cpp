/* Minimal ffrAPI stub implementation for offline link/CLI smoke tests.
 * Real builds link against Synopsys libnffr.so instead. Every entry fails. */
#include <cstddef>

#include "ffrAPI_stub.h"

static str_T g_scale = "1ns";

bool_T ffrObject::ffrIsFSDB(str_T) { return FALSE; }
ffrObject *ffrObject::ffrOpen3(str_T) { return NULL; }
str_T ffrObject::ffrGetScaleUnit(void) { return g_scale; }
void ffrObject::ffrSetTreeCBFunc(fsdbTreeCBFuncT, void *) {}
fsdbRC ffrObject::ffrReadScopeVarTree(void) { return FSDB_RC_FAILURE; }
fsdbRC ffrObject::ffrAddToSignalList(fsdbVarIdcode) { return FSDB_RC_FAILURE; }
fsdbRC ffrObject::ffrLoadSignals(void) { return FSDB_RC_FAILURE; }
fsdbRC ffrObject::ffrUnloadSignals(void) { return FSDB_RC_FAILURE; }
ffrVCTrvsHdl ffrObject::ffrCreateVCTraverseHandle(fsdbVarIdcode) { return NULL; }
ffrTimeBasedVCTrvsHdl ffrObject::ffrCreateTimeBasedVCTrvsHdl(uint_T,
                                                             fsdbVarIdcode *) {
    return NULL;
}
fsdbRC ffrObject::ffrGetMaxVarIdcode(void) { return FSDB_RC_FAILURE; }
fsdbRC ffrObject::ffrGetMaxFsdbTag64(fsdbTag64 *) { return FSDB_RC_FAILURE; }
fsdbRC ffrObject::ffrClose(void) { return FSDB_RC_FAILURE; }

fsdbRC ffrVCTrvsHdl_t::ffrHasIncoreVC(void) { return FALSE; }
fsdbRC ffrVCTrvsHdl_t::ffrGotoXTag(void *) { return FSDB_RC_FAILURE; }
fsdbRC ffrVCTrvsHdl_t::ffrGotoNextVC(void) { return FSDB_RC_FAILURE; }
fsdbRC ffrVCTrvsHdl_t::ffrGotoPrevVC(void) { return FSDB_RC_FAILURE; }
fsdbRC ffrVCTrvsHdl_t::ffrGetXTag(void *) { return FSDB_RC_FAILURE; }
fsdbRC ffrVCTrvsHdl_t::ffrGetVC(byte_T **) { return FSDB_RC_FAILURE; }
fsdbRC ffrVCTrvsHdl_t::ffrGetMaxXTag(void *) { return FSDB_RC_FAILURE; }
void ffrVCTrvsHdl_t::ffrFree(void) {}

fsdbRC ffrTimeBasedVCTrvsHdl_t::ffrGotoNextVC(void) { return FSDB_RC_FAILURE; }
fsdbRC ffrTimeBasedVCTrvsHdl_t::ffrGetVarIdcodeXTagVCSeqNum(fsdbVarIdcode *,
                                                            fsdbXTag *,
                                                            byte_T **,
                                                            fsdbSeqNum *) {
    return FSDB_RC_FAILURE;
}
void ffrTimeBasedVCTrvsHdl_t::ffrFree(void) {}
