# Phase 1 Context

全部选择已由用户批准：spawn而非fork；构建与execute均在worker；明确import target与JSON快照；事件/hook请求回复通过受控IPC；合作取消后terminate/kill并join；退出才释放槽位。强杀报告外部副作用不可撤销。无隐式线程fallback。脚本工厂作为SDK/嵌入测试边界。

现有RunManager是Task.cancel后备，SessionRunner线程桥，不符合强终止要求。新worker必须独立建立，后续阶段替换生产路径，不能提前宣称接入完成。
