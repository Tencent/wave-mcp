// Verilator testbench for ibex_core -> FST. Minimal instruction-fetch model.
#include "Vibex_core.h"
#include "verilated.h"
#include "verilated_fst_c.h"
#include <cstdlib>

static vluint64_t now = 0;
static Vibex_core* dut;
static VerilatedFstC* tfp;

static void eval_dump() { dut->eval(); tfp->dump(now); }

static void tick() {
    dut->clk_i = 0; eval_dump(); now += 5000;
    dut->clk_i = 1; eval_dump(); now += 5000;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Verilated::traceEverOn(true);
    dut = new Vibex_core;
    tfp = new VerilatedFstC;
    dut->trace(tfp, 99);
    const char* fst = getenv("FST_OUT");
    tfp->open(fst ? fst : "/tmp/wave_verify/ibex_core.fst");

    // tie-offs
    dut->rst_ni = 0;
    dut->hart_id_i = 0;
    dut->boot_addr_i = 0x80000000u;
    dut->fetch_enable_i = 0xf;   // MuBi4True-ish (let core run)
    dut->irq_software_i = 0; dut->irq_timer_i = 0; dut->irq_external_i = 0;
    dut->irq_nm_i = 0; dut->irq_fast_i = 0; dut->debug_req_i = 0;
    dut->instr_gnt_i = 1; dut->instr_rvalid_i = 0; dut->instr_err_i = 0;
    dut->instr_rdata_i = 0x00000013u;  // NOP (addi x0,x0,0)
    dut->data_gnt_i = 1; dut->data_rvalid_i = 0; dut->data_err_i = 0;
    dut->data_rdata_i = 0;
    dut->ic_scr_key_valid_i = 0;
    dut->rf_rdata_a_ecc_i = 0; dut->rf_rdata_b_ecc_i = 0;

    for (int i = 0; i < 6; i++) tick();
    dut->rst_ni = 1;

    // run: respond to instruction fetches with NOP, 1-cycle latency
    for (int c = 0; c < 60; c++) {
        // model: whatever was requested last cycle returns valid this cycle
        dut->instr_rvalid_i = dut->instr_req_o;
        dut->data_rvalid_i = dut->data_req_o;
        // vary instruction a bit: alternate NOP and addi x1,x1,1
        dut->instr_rdata_i = (c % 4 == 0) ? 0x00108093u /*addi x1,x1,1*/
                                          : 0x00000013u /*nop*/;
        tick();
    }

    tfp->close();
    delete tfp; delete dut;
    return 0;
}
