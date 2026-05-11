using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// Grid MM v8 Launcher — trend-following (InvertedLogic = true).
/// 
/// Отличия от V7:
/// - SAR↑ > EMA → LONG (по тренду), SAR↓ < EMA → SHORT
/// - Лучше на трендовых инструментах: GOLD, MIX, RTS
/// - State file: /tmp/v8-state.json
/// - API prefix: /strategy/grid-mm-v8/
/// 
/// Вся логика исполнения (grid, TP, MainLoop) — в базовом GridMmV7Launcher.
/// </summary>
public class GridMmV8Launcher : GridMmV7Launcher
{
    public GridMmV8Launcher(FinamConnector broker, string? stateFile = null)
        : base(broker, new GridMmV7Strategy(new GridMmV7Strategy.Config { InvertedLogic = true }), useV8: true, stateFile ?? "/tmp/v8-state.json")
    {
    }
    
    /// <summary>
    /// Создать V8 с кастомными параметрами стратегии
    /// </summary>
    public GridMmV8Launcher(FinamConnector broker, GridMmV7Strategy.Config config, string? stateFile = null)
        : base(broker, new GridMmV7Strategy(config), useV8: true, stateFile ?? "/tmp/v8-state.json")
    {
    }
}
