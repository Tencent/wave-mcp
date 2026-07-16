// Verilator testbench for tlul_socket_1n -> FST.
#include "Vtlul_socket_1n.h"
#include "verilated.h"
#include "verilated_fst_c.h"
#include <cstdlib>

static vluint64_t now = 0;
static Vtlul_socket_1n* dut;
static VerilatedFstC* tfp;

static void tick() {
    dut->clk_i = 0; dut->eval(); tfp->dump(now); now += 5000;
    dut->clk_i = 1; dut->eval(); tfp->dump(now); now += 5000;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Verilated::traceEverOn(true);
    dut = new Vtlul_socket_1n;
    tfp = new VerilatedFstC;
    dut->trace(tfp, 99);
    const char* fst = getenv("FST_OUT");
    tfp->open(fst ? fst : "/tmp/wave_verify/socket_1n.fst");

    dut->rst_ni = 0;
    dut->dev_select_i = 0;
    for (int i = 0; i < 5; i++) dut->tl_h_i[i] = 0;
    for (int d = 0; d < 4; d++) for (int i = 0; i < 3; i++) dut->tl_d_i[d][i] = 0;
    for (int i = 0; i < 4; i++) tick();
    dut->rst_ni = 1;
    for (int i = 0; i < 2; i++) tick();

    // drive a few host requests toward device 0, with device responding ready
    for (int r = 0; r < 8; r++) {
        dut->dev_select_i = r % 4;
        dut->tl_h_i[0] = 0x1 | (r << 8);    // a_valid + payload-ish
        dut->tl_d_i[r % 4][0] = 0x3;        // device a_ready / d_valid bits
        tick();
        dut->tl_h_i[0] = 0x0;
        dut->tl_d_i[r % 4][0] = 0x0;
        tick();
    }
    for (int i = 0; i < 8; i++) tick();

    tfp->close();
    delete tfp; delete dut;
    return 0;
}
