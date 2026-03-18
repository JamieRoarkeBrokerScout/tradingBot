export interface Trade {
    id: number;
    entry_time: string;
    exit_time: string;
    instrument: string;
    direction: number;
    entry_price: number;
    exit_price: number;
    exit_reason: string;
    raw_pl: number;
    pl_R: number;
    entry_units?: number;
}

export interface AlltimeStats {
    total_trades: number;
    total_pl: number;
    total_wins: number;
    total_losses: number;
    win_rate: number;
    avg_win: number;
    avg_loss: number;
    profit_factor: number | null;
}

export interface Stats {
    daily_pnl: number;
    trades_today: number;
    wins: number;
    losses: number;
    alltime?: AlltimeStats;
    by_strategy?: Record<string, { trades: number; pl: number; wins: number; losses: number }>;
    today_by_strategy?: Record<string, number>;
}

export type DisplayTrade = Trade & {
    type: string;
    profit: number;
    date: string;
    duration: string;
    reason: string;
    size: string;
    stock: string;
};

export interface OpenTrade {
    id: number;
    trade_key: string;
    strategy: string;
    instrument: string;
    direction: number;
    units: number;
    entry_price: number;
    entry_time: string;
    current_price?: number | null;
    unrealized_pl?: number | null;
    stop_price?: number | null;
    tp_price?: number | null;
}

export interface AccountInfo {
    account_id: string;
    balance: number;
    nav: number;
    unrealized_pl: number;
    margin_used: number;
    margin_available: number;
    margin_pct: number;
    open_trade_count: number;
    currency: string;
    error?: string;
}

export type AccountData = Record<string, AccountInfo>;

export interface StrategyState {
    enabled: boolean;
}

export interface StrategiesResponse {
    runner_running: boolean;
    strategies: {
        stat_arb:      StrategyState;
        momentum:      StrategyState;
        vol_premium:   StrategyState;
        crypto:        StrategyState;
        daily_target:  StrategyState;
        scalp:         StrategyState;
    };
}
