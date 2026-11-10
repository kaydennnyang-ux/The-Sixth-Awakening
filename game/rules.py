"""核心规则：移动、推箱、同时结算、机关与胜利判定。

这一层完全不接触输入输出，所以单元测试可以直接调用它，
也方便以后把字符界面换成别的界面而不用改规则。

关键约定（对应设计文档第六节）：
    * 撞墙 / 撞箱子 / 撞实体装置时，角色留在原地，但仍然消耗一个回合；
    * 玩家与回声不能重叠，也不能交换位置；发生冲突时双方都留在原地；
    * 机关状态永远由"占位"推导，不靠计数器累加，避免出现残留状态。
"""

from typing import Optional, Tuple

from .constants import (
    TILE_CONSOLE,
    TILE_RECORDER,
    Action,
    Phase,
)
from .level import Level
from .position import Pos
from .state import GameState
from .tilemap import TileMap


# ==========================================================================
# 基础查询
# ==========================================================================
def blocked_reason(tilemap: TileMap, state: GameState, pos: Pos) -> Optional[str]:
    """判断某格是否无法站人，返回原因文字；可以站则返回 None。

    注意：这里只考虑"房间地形 + 木箱"，不判断另一个角色。
    玩家与回声之间的冲突在回合结算时统一处理（见 resolve_execute_turn）。
    """
    if not tilemap.in_bounds(pos):
        return "the edge of the room"
    if tilemap.is_wall(pos):
        return "a wall"

    tile = tilemap.tile(pos)
    if tile == TILE_CONSOLE:
        return "the console"
    if tile == TILE_RECORDER and not state.recorder_taken:
        return "the recorder"
    if pos == state.box:
        return "the crate"
    return None


def update_switches(level: Level, state: GameState) -> bool:
    """按当前占位重新计算所有机关，返回"这一回合门是否刚刚开启"。

    每次角色或箱子移动后都要调用一次，不要各自累加状态。
    """
    # 配重板被压住 => 记录器台通过电（永久解锁，跨重置保留）
    if state.box == level.weight_plate:
        state.power_ever = True

    left_on, right_on = state.exit_plates_pressed(level)
    both_on = left_on and right_on

    just_opened = both_on and not state.door_open
    if both_on:
        # 设计文档：门一旦被两块板同时激活就保持开启（latched），
        # 这样玩家离开压力板后仍然可以走向出口。
        state.door_open = True
    return just_opened


def check_win(level: Level, state: GameState) -> bool:
    """门已开启且玩家站在出口格上 => 通关。"""
    if state.door_open and state.player == level.exit_tile:
        state.win = True
        state.phase = Phase.WIN
        return True
    return False


# ==========================================================================
# 探索阶段
# ==========================================================================
def interact(level: Level, state: GameState) -> Tuple[str, str]:
    """处理"交互"指令。

    返回 (事件类型, 提示文字)。事件类型供上层界面决定是否打开控制台菜单：
        "pickup"        取走了记忆晶片
        "console"       站在记录器台旁边且装置可用
        "console_locked" 站在记录器台旁边但装置还没通电/没取晶片
        "nothing"       附近没有可交互的东西
    """
    player = state.player

    # 1) 记忆晶片：相邻即可拾取（晶片所在格本身是实体，不能站上去）
    if not state.recorder_taken and player.is_adjacent_to(level.recorder_item):
        state.recorder_taken = True
        return (
            "pickup",
            "You lift the memory chip from its bracket. "
            "Five slots. Not one more.",
        )

    # 2) 记录器台：必须先做过"箱子 -> 配重板"这一步
    if player.is_adjacent_to(level.console):
        if state.device_ready():
            return ("console", "The console hums, waiting for your command.")
        if not state.power_ever:
            return (
                "console_locked",
                "The console is dark. The weight plate on the east wall "
                "must supply power first.",
            )
        return (
            "console_locked",
            "The console has power, but no recorder is plugged in. "
            "Find the memory chip.",
        )

    return ("nothing", "There is nothing within reach.")


def step_explore(level: Level, tilemap: TileMap, state: GameState, action: Action) -> str:
    """在探索阶段走一步，返回给玩家看的反馈文字。"""
    if action is Action.WAIT:
        return "You wait. The room stays still."

    origin = state.player
    target = origin.neighbour(action)

    # --- 目标是木箱：尝试推它 ---
    if target == state.box:
        beyond = target.neighbour(action)          # 箱子会去的那一格
        reason = blocked_reason(tilemap, state, beyond)
        if reason is not None:
            return "The crate is jammed against {0}.".format(reason)
        state.box = beyond
        state.player = target
        opened = update_switches(level, state)
        if state.box == level.weight_plate:
            return (
                "You shove the crate onto the weight plate. "
                "Somewhere inside the wall, a relay closes."
            )
        if opened:  # 理论上推箱不会直接开门，保底处理
            return "Both exit plates are pressed. The door locks open."
        return "You shove the crate forward."

    # --- 普通移动 ---
    reason = blocked_reason(tilemap, state, target)
    if reason is not None:
        return "You cannot step there: {0}.".format(reason)

    state.player = target
    update_switches(level, state)
    return "You move {0}.".format(action.value)


# ==========================================================================
# 执行阶段（玩家 + 回声同时结算）
# ==========================================================================
def step_execute(level: Level, tilemap: TileMap, state: GameState, action: Action) -> str:
    """执行阶段走一个回合。

    顺序严格按设计文档的伪代码：
        1. 读回合开始状态，分别算出玩家和回声想去哪一格；
        2. 检查墙、箱子、实体装置；
        3. 处理双方冲突（重叠 / 交换位置）；
        4. 统一写入位置，再统一结算机关。
    绝不能"移动玩家 -> 结算 -> 移动回声 -> 再结算"，否则开关会闪烁误判。
    """
    if state.echo is None:
        state.echo = level.echo_start

    # 回声这一步要执行的动作：列表用完后一直等待
    if state.echo_index < len(state.actions):
        echo_action = state.actions[state.echo_index]
    else:
        echo_action = Action.WAIT

    # ---------- 1. 计算意图 ----------
    player_target = state.player.neighbour(action)
    echo_target = state.echo.neighbour(echo_action)

    player_moves = action is not Action.WAIT
    echo_moves = echo_action is not Action.WAIT

    notes = []

    # ---------- 2. 地形检查 ----------
    if player_moves:
        reason = blocked_reason(tilemap, state, player_target)
        if reason is not None:
            player_moves = False
            notes.append("Your way is blocked by {0}.".format(reason))
    if echo_moves:
        reason = blocked_reason(tilemap, state, echo_target)
        if reason is not None:
            echo_moves = False
            notes.append("The echo is blocked by {0}.".format(reason))

    # ---------- 3. 双方冲突 ----------
    swap = (
        player_moves
        and echo_moves
        and player_target == state.echo
        and echo_target == state.player
    )
    final_player = player_target if player_moves else state.player
    final_echo = echo_target if echo_moves else state.echo

    if swap or final_player == final_echo:
        if swap:
            notes.append("You and the echo try to pass through each other.")
        else:
            notes.append("You and the echo reach for the same tile.")
        notes.append("Both of you hold still this turn.")
        player_moves = False
        echo_moves = False
        final_player = state.player
        final_echo = state.echo

    # ---------- 4. 统一写入 ----------
    state.player = final_player
    state.echo = final_echo
    if state.echo_index < len(state.actions):
        state.echo_index += 1
    state.turn += 1

    # ---------- 5. 统一结算机关与胜负 ----------
    opened = update_switches(level, state)
    if opened:
        notes.append("BOTH EXIT PLATES PRESSED - the door locks open!")

    if check_win(level, state):
        return "You step through the door."

    if not notes:
        # 默认反馈：把玩家和回声这一步各自做了什么说清楚
        notes.append(
            "You {0}; the echo {1}.".format(
                "wait" if action is Action.WAIT else "move " + echo_action.value,
                "waits" if echo_action is Action.WAIT else "replays " + action.value,
            )
        )
    return " ".join(notes)
