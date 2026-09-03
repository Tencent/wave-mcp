# 波形查看器截图

agent 分析完一个问题、把结论呈现给你时，浏览器里实际长什么样。下面每张图都是
[examples/viewer_demos](../examples/viewer_demos) 四个 demo 场景的真实抓图，不是效果图：
信号分组、总线进制、颜色、分析说明弹窗，全部由 `open_wave_view` / `update_wave_view` 生成。

每个场景背后的调试推理（哪一次工具调用发现了什么）写在
[demo README](../examples/viewer_demos/README.md) 里，本页只讲你看到的画面。

## X 态传播

`data_out` 标红、每个包都读出 `xx`，而 `fsm_state` 还在 IDLE→SHIFT→FLUSH 正常跳：状态机是好的，
数据通路被污染了。分组把根因 `byte_cnt` 和症状 `data_out` 摆在一起，`byte_cnt` 全程是 `x`，
X 经 `data_out[7:5]` 一路传到输出。右下角的分析说明弹窗里是 agent 的结论，带置信度标注和
`trace_x` / `signal_drivers` 的证据。

![波形查看器中的 X 态传播](images/viewer/xprop.png)

## 状态机死锁

`req` 从 145ns 起一直是高，`ack` 再也不来，`state` 卡在 `1`（`WAIT_ACK`）不动，`rd_count` 冻在 `03`。
两个游标分别停在最后一次完成的读（125ns）和 `req` 永久拉高的那一拍（145ns），握手信号和
状态、计数分组摆开，卡死的位置一眼可见。

![波形查看器中的状态机死锁](images/viewer/fsm_stuck.png)

## 跨时钟域丢脉冲

快时钟域的 `pulse_fast` 发出六个脉冲，`pulse_seen` 只收到两个，`pulse_count` 停在 `02`。
按时钟域分组后丢失一目了然：游标停在 830ns 的丢失脉冲上，这个 20ns 宽的脉冲整段落在两个
`clk_slow` 上升沿之间（慢时钟周期 50ns），慢时钟域根本没机会采到它。

![波形查看器中的跨时钟域丢脉冲](images/viewer/cdc.png)

## pass/fail 首分歧

两份波形上下各占一个 pane，时间轴对齐。RTL 相同、激励相同，只差一处 CRC 抽头写错
（fail 版少了 `^ data[0]`）。`crc` 从第一个 valid 节拍就分道扬镳（35ns 处 `0110` vs `0111`，
上面走 `6/9/3/0`，下面走 `7/d/c/9`），而 `crc_err` 只在失败那次于 425ns 拉高。
两个游标分别标出首分歧和 `crc_err` 拉高的位置，这正是 `diff_waveforms` 为首分歧定位铺好的视图。

![波形查看器中的 pass/fail 首分歧](images/viewer/crc_diff.png)

## 关于界面语言

分析说明弹窗的内容**由 agent 自己决定用什么语言**，wave-mcp 不做任何限制，上面几张图是
中文的效果。

信号分组名是个例外，它画在 Surfer 的 WASM 画布里，那份字体图集不含 CJK 字形，所以写中文
会显示成方块（分组本身照常生效，只是标题不可读），建议用简短的 ASCII 词。名字里的空格由
服务端自动转成下划线：sucl 解析器不接受带空格的参数，原样发过去整个分组标题会被静默丢弃。

## 自己复现一遍

本页内容都能从一份干净的 clone 复现出来。先构建 demo 波形，然后要么重新抓图，
要么直接交互式打开任一场景。

前置依赖：PATH 上有 `iverilog` 与 `vcd2fst`，以及查看器可选资产包
（`pip install -e ".[viewer]"`，或设 `WAVE_MCP_VIEWER_ASSETS`）。

```bash
cd examples/viewer_demos
./make_all.sh                 # iverilog -> vcd2fst -> build_session
```

重新生成本页全部截图（需要 `playwright` 及其 chromium：
`pip install playwright && playwright install chromium`）：

```bash
python3 examples/viewer_demos/capture_screenshots.py
python3 examples/viewer_demos/capture_screenshots.py cdc   # 只抓一个
```

图片写入 `docs/images/viewer/`。脚本以 headless chromium 运行，会等到 Surfer 的 WASM
画布真正绘制完成才截图；缺 playwright 或缺查看器资产时干净跳过（退出码 0）。

想动手摆弄某个场景而不是看静态图，带 `--hold` 跑 demo，然后打开它打印的 URL：

```bash
python3 examples/viewer_demos/demo3_cdc.py --hold
```

也可以直接打开预置波形，不带脚本里那些分析说明：

```bash
wave-view examples/viewer_demos/waves/cdc.fst \
    --signals cdc_tb.dut.pulse_fast cdc_tb.dut.pulse_seen cdc_tb.dut.clk_slow
```

在远程机器上跑时，把查看器打印的端口转发出来
（`ssh -L 8080:127.0.0.1:<port> <host>`）再在本地打开。完整的 CLI 与工具参考见
[波形查看器指南](WAVE_VIEWER.md)。
