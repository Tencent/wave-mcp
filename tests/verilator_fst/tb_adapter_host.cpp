// Verilator testbench for tlul_adapter_host -> FST waveform.
#include "Vtlul_adapter_host.h"
#include "verilated.h"
#include "verilated_fst_c.h"
#include <cstdlib>

static vluint64_t now = 0;
static Vtlul_adapter_host* dut;
static VerilatedFstC* tfp;

// 5ns half-period -> 10ns clock; FST timescale is 1ps so multiply by 1000.
static void tick() {
    dut->clk_i = 0; dut->eval(); tfp->dump(now); now += 5000;
    dut->clk_i = 1; dut->eval(); tfp->dump(now); now += 5000;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Verilated::traceEverOn(true);
    dut = new Vtlul_adapter_host;
    tfp = new VerilatedFstC;
    dut->trace(tfp, 99);
    const char* fst = getenv("FST_OUT");
    tfp->open(fst ? fst : "/tmp/wave_verify/adapter_host.fst");

    // reset
    dut->rst_ni = 0; dut->req_i = 0; dut->we_i = 0;
    dut->addr_i = 0; dut->wdata_i = 0; dut->be_i = 0xf;
    dut->instr_type_i = 0;
    dut->tl_i[0] = 0; dut->tl_i[1] = 0; dut->tl_i[2] = 0;
    for (int i = 0; i < 4; i++) tick();
    dut->rst_ni = 1;
    for (int i = 0; i < 2; i++) tick();

    // issue a few read requests with handshake
    for (int r = 0; r < 6; r++) {
        dut->req_i = 1;
        dut->addr_i = 0x1000 + r * 4;
        dut->we_i = (r % 2);
        dut->wdata_i = 0xdead0000 + r;
        tick();
        // simulate device asserting a_ready / d_valid (low bits of d2h struct)
        dut->tl_i[0] = 0x3;  // toggle some d2h handshake bits
        tick();
        dut->tl_i[0] = 0x0;
    }
    dut->req_i = 0;
    for (int i = 0; i < 6; i++) tick();

    tfp->close();
    delete tfp; delete dut;
    return 0;
}
