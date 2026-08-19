msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"


root@61f7f4017bb7:/workspace# msagent config --show
^[]11;rgb:fafa/fafa/fdfd^[\      Current Configuration      
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ Setting      ┃ Value          ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ Agent        │ Profiler       │
│ LLM Provider │ openai         │
│ Model        │ gpt-4o-mini    │
│ API Key      │ Not set        │
│ API Key Env  │ OPENAI_API_KEY │
│ Base URL     │ Default        │
│ Max Tokens   │ Auto           │
│ MCP Servers  │ 1              │
└──────────────┴────────────────┘
                         MCP Servers                         
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Name       ┃ Command    ┃ Arguments            ┃ Status   ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ msprof-mcp │ msprof-mcp │ None                 │ Enabled  │
│ tavily-mcp │ npx        │ -y tavily-mcp@latest │ Disabled │
└────────────┴────────────┴──────────────────────┴──────────┘

Config dir: /workspace/.msagent
root@61f7f4017bb7:/workspace# 11;rgb:fafa/fafa/fdfdroot@61f7f4017bb7:/workspace# msagent config --show
^[]11;rgb:fafa/fafa/fdfd^[\      Current Configuration    


root@61f7f4017bb7:/workspace# msagent
^[]11;rgb:fafa/fafa/fdfd^[\你好

╭──────────────────────────── * Welcome to msAgent v26.1.0a2 ────────────────────────────╮
│                                                                                        │
│            ███╗   ███╗███████╗ █████╗  ██████╗ ███████╗███╗   ██╗████████╗             │
│            ████╗ ████║██╔════╝██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝             │
│            ██╔████╔██║███████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║                │
│            ██║╚██╔╝██║╚════██║██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║                │
│            ██║ ╚═╝ ██║███████║██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║                │
│            ╚═╝     ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝                │
│                                                                                        │
│  msAgent 是 MindStudio 一站式调试调优 Agent，支持性能、精度、算子等场景问题定位。      │
│  Agent: Profiler - Ascend NPU profiling analysis agent with msprof-mcp-first workflow  │
│  Model: gpt-4o-mini (openai)                                                           │
│  MCP (1)                                                                               │
│    - msprof-mcp                                                                        │
│  Skills (9)                                                                            │
│    - ascend-cluster-fast-slow-rank-detector                                            │
│    - ascend-communication-analysis                                                     │
│    - ascend-computation-analysis                                                       │
│    - ascend-msprof-analyze-cli                                                         │
│    - ascend-profiler-data-validation                                                   │
│    - ascend-profiler-db-explorer                                                       │
│    - ascend-schedule-analysis                                                          │
│    - github-raw-fetch                                                                  │
│    - op-mfu-calculator                                                                 │
│                                                                                        │
╰────────────────────────────────────────────────────────────────────────────────────────╯

Profiler > ]11;rgb:fafa/fafa/fdfd你好 


基于已采集的Profilng数据，使用Agent辅助分析调度类问题，例如询问：“从当前Profiling数据来看，有无集群快慢卡，有什么关键证据”，“造成快慢卡的原因是什么”，“评估快慢卡问题造成的影响，拖慢了多少时间”等多轮交互，记录每次对话的输出，判断是否符合预期。