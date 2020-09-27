"""Group player provider: enables grouping of all playertypes."""

import asyncio
import logging
from typing import List

from music_assistant.constants import CONF_GROUP_DELAY
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import DeviceInfo, PlaybackState, Player
from music_assistant.models.playerprovider import PlayerProvider

PROV_ID = "group_player"
PROV_NAME = "Group player creator"
LOGGER = logging.getLogger(PROV_ID)

CONF_PLAYER_COUNT = "group_player_count"
CONF_PLAYERS = "group_player_players"
CONF_MASTER = "group_player_master"

CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_PLAYER_COUNT,
        entry_type=ConfigEntryType.INT,
        description_key=CONF_PLAYER_COUNT,
        default_value=1,
        range=(0, 10),
    )
]


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = GroupPlayerProvider()
    await mass.async_register_provider(prov)


class GroupPlayerProvider(PlayerProvider):
    """PlayerProvider which allows users to group players."""

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return CONFIG_ENTRIES

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        conf = self.mass.config.providers[PROV_ID]
        for index in range(conf[CONF_PLAYER_COUNT]):
            player = GroupPlayer(self.mass, index)
            self.mass.add_job(self.mass.player_manager.async_add_player(player))
        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit. Called on shutdown."""
        for player in self.players:
            await player.async_cmd_stop()


class GroupPlayer(Player):
    """Model for a group player."""

    def __init__(self, mass: MusicAssistantType, player_index: int):
        """Initialize."""
        self.mass = mass
        self._player_index = player_index
        self._player_id = f"group_player_{player_index}"
        self._provider_id = PROV_ID
        self._name = f"Group Player {player_index}"
        self._powered = False
        self._state = PlaybackState.Stopped
        self._available = True
        self._current_uri = ""
        self._volume_level = 0
        self._muted = False
        self.connected_clients = {}
        self.stream_task = None
        self.sync_task = None

    @property
    def player_id(self) -> str:
        """Return player id of this player."""
        return self._player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return self._provider_id

    @property
    def name(self) -> str:
        """Return name of the player."""
        return self._name

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._powered

    @property
    def state(self) -> PlaybackState:
        """Return current PlaybackState of player."""
        return self._state

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return True

    @property
    def current_uri(self) -> str:
        """Return currently loaded uri of player (if any)."""
        return self._current_uri

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._volume_level

    @property
    def muted(self) -> bool:
        """Return current mute state of player."""
        return self._muted

    @property
    def elapsed_time(self):
        """Return elapsed timefor first child player."""
        if self.state in [PlaybackState.Playing, PlaybackState.Paused]:
            for player_id in self.group_childs:
                player = self.mass.player_manager.get_player(player_id)
                if player:
                    return player.elapsed_time
        return 0

    @property
    def should_poll(self):
        """Return True if this player should be polled for state."""
        return self.state in [PlaybackState.Playing, PlaybackState.Paused]

    @property
    def is_group_player(self) -> bool:
        """Return True if this player is a group player."""
        return True

    @property
    def group_childs(self):
        """Return group childs of this group player."""
        player_conf = self.mass.config.get_player_config(self.player_id)
        if player_conf and player_conf.get(CONF_PLAYERS):
            return player_conf[CONF_PLAYERS]
        return []

    @property
    def device_info(self) -> DeviceInfo:
        """Return deviceinfo."""
        return DeviceInfo(
            model="Group Player",
            manufacturer=PROV_ID,
        )

    @property
    def config_entries(self):
        """Return config entries for this group player."""
        all_players = [
            {"text": item.name, "value": item.player_id}
            for item in self.mass.player_manager.players
            if item.player_id is not self._player_id
        ]
        selected_players = self.mass.config.get_player_config(self.player_id).get(
            CONF_PLAYERS, []
        )
        default_master = ""
        if selected_players:
            default_master = selected_players[0]
        return [
            ConfigEntry(
                entry_key=CONF_PLAYERS,
                entry_type=ConfigEntryType.STRING,
                default_value=[],
                values=all_players,
                description_key=CONF_PLAYERS,
                multi_value=True,
            ),
            ConfigEntry(
                entry_key=CONF_MASTER,
                entry_type=ConfigEntryType.STRING,
                default_value=default_master,
                values=selected_players,
                description_key=CONF_MASTER,
                multi_value=False,
                depends_on=CONF_MASTER,
            ),
        ]

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_cmd_play_uri(self, uri: str):
        """Play the specified uri/url on the player."""
        await self.async_cmd_stop()
        self._current_uri = uri
        self._state = PlaybackState.Playing
        self._powered = True
        # forward this command to each child player
        # TODO: Only start playing on powered players ?
        # Monitor if a child turns on and join it to the sync ?
        for child_player_id in self.group_childs:
            child_player = self.mass.player_manager.get_player(child_player_id)
            if child_player:
                queue_stream_uri = f"{self.mass.web.internal_url}/stream/group/{self.player_id}?player_id={child_player_id}"
                await child_player.async_cmd_play_uri(queue_stream_uri)
        self.update_state()
        self.stream_task = self.mass.add_job(self.async_queue_stream_task())

    async def async_cmd_stop(self) -> None:
        """Send STOP command to player."""
        self._state = PlaybackState.Stopped
        if self.stream_task:
            # cancel existing stream task if any
            self.stream_task.cancel()
            self.connected_clients = {}
            await asyncio.sleep(0.5)
        if self.sync_task:
            self.sync_task.cancel()
        # forward this command to each child player
        # TODO: Only forward to powered child players
        for child_player_id in self.group_childs:
            child_player = self.mass.player_manager.get_player(child_player_id)
            if child_player:
                await child_player.async_cmd_stop()
        self.update_state()

    async def async_cmd_play(self) -> None:
        """Send PLAY command to player."""
        if not self.state == PlaybackState.Paused:
            return
        # forward this command to each child player
        for child_player_id in self.group_childs:
            child_player = self.mass.player_manager.get_player(child_player_id)
            if child_player:
                await child_player.async_cmd_play()
        self._state = PlaybackState.Playing
        self.update_state()

    async def async_cmd_pause(self):
        """Send PAUSE command to player."""
        # forward this command to each child player
        for child_player_id in self.group_childs:
            child_player = self.mass.player_manager.get_player(child_player_id)
            if child_player:
                await child_player.async_cmd_pause()
        self._state = PlaybackState.Paused
        self.update_state()

    async def async_cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        self._powered = True
        self.update_state()

    async def async_cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        await self.async_cmd_stop()
        self._powered = False
        self.update_state()

    async def async_cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to player.

            :param volume_level: volume level to set (0..100).
        """
        for child_player_id in self.group_childs:
            child_player = self.mass.player_manager.get_player(child_player_id)
            if child_player and child_player.powered:
                await child_player.async_cmd_volume_set(volume_level)

    async def async_cmd_volume_mute(self, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        for child_player_id in self.group_childs:
            child_player = self.mass.player_manager.get_player(child_player_id)
            if child_player and child_player.powered:
                await child_player.async_cmd_volume_mute(is_muted)
        self.muted = is_muted

    async def subscribe_stream_client(self, child_player_id):
        """Handle streaming to all players of a group. Highly experimental."""

        # each connected client gets its own sox process to convert incoming pcm samples
        # to flac (which is streamed to the player).
        args = [
            "sox",
            "-t",
            "s32",
            "-c",
            "2",
            "-r",
            "96000",
            "-",
            "-t",
            "flac",
            "-C",
            "0",
            "-",
        ]
        sox_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        chunk_size = 2880000  # roughly 5 seconds of flac @ 96000/32
        try:
            # report this client as connected
            self.connected_clients[child_player_id] = sox_proc.stdin
            LOGGER.debug(
                "[%s] child player connected: %s",
                self.player_id,
                child_player_id,
            )
            # yield flac chunks from stdout to the http streamresponse
            while True:
                try:
                    chunk = await sox_proc.stdout.readexactly(chunk_size)
                    yield chunk
                except asyncio.IncompleteReadError as exc:
                    chunk = exc.partial
                    yield chunk
                    break
        except (GeneratorExit, Exception):  # pylint: disable=broad-except
            LOGGER.warning(
                "[%s] child player aborted stream: %s", self.player_id, child_player_id
            )
            self.connected_clients.pop(child_player_id, None)
            await sox_proc.communicate()
            sox_proc.terminate()
            if sox_proc and sox_proc.returncode is None:
                await sox_proc.wait()
        else:
            self.connected_clients.pop(child_player_id, None)
            LOGGER.debug(
                "[%s] child player completed streaming: %s",
                self.player_id,
                child_player_id,
            )

    async def async_queue_stream_task(self):
        """Handle streaming queue to connected child players."""
        ticks = 0
        while ticks < 60 and len(self.connected_clients) != len(self.group_childs):
            # TODO: Support situation where not alle clients of the group are powered
            await asyncio.sleep(0.1)
            ticks += 1
        if not self.connected_clients:
            LOGGER.warning("no clients!")
            return
        LOGGER.debug(
            "start queue stream with %s connected clients", len(self.connected_clients)
        )
        self.sync_task = asyncio.create_task(self.__synchronize_players())

        received_milliseconds = 0
        received_seconds = 0
        async for audio_chunk in self.mass.stream_manager.async_queue_stream_pcm(
            self.player_id, sample_rate=96000, bit_depth=32
        ):
            received_seconds += 1
            received_milliseconds += 1000
            chunk_size = len(audio_chunk)
            start_bytes = 0

            # make sure we still have clients connected
            if not self.connected_clients:
                LOGGER.warning("no more clients!")
                return

            # send the audio chunk to all connected players
            for child_player_id, writer in self.connected_clients.items():

                # work out startdelay
                if received_seconds == 1:
                    player_delay = self.mass.config.player_settings[
                        child_player_id
                    ].get(CONF_GROUP_DELAY, 0)
                    if player_delay:
                        start_bytes = int(
                            ((player_delay - received_milliseconds) / 1000) * chunk_size
                        )
                    else:
                        start_bytes = 0

                # send the data to the client
                try:
                    writer.write(audio_chunk[start_bytes:])
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError, AssertionError):
                    pass  # happens at client disconnect

            if not self.connected_clients:
                LOGGER.warning("no more clients!")
                return

    async def __synchronize_players(self):
        """Handle drifting/lagging by monitoring progress and compare to master player."""

        master_player_id = self.mass.config.player_settings[self.player_id].get(
            CONF_MASTER
        )
        if not master_player_id:
            LOGGER.warning("Synchronization of playback aborted: no master player.")
            return
        else:
            LOGGER.debug(
                "Synchronize playback of group using master player %s", master_player_id
            )
        master_player = self.mass.player_manager.get_player(master_player_id)

        # wait until master is playing
        while master_player.state != PlaybackState.Playing:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)

        prev_lags = {}
        prev_drifts = {}

        while self.connected_clients:

            # check every 2 seconds for player sync
            await asyncio.sleep(0.5)

            for child_player_id in self.connected_clients:

                if child_player_id == master_player_id:
                    continue
                child_player = self.mass.player_manager.get_player(child_player_id)

                if (
                    not child_player
                    or child_player.state != PlaybackState.Playing
                    or child_player.elapsed_milliseconds is None
                ):
                    continue

                if child_player_id not in prev_lags:
                    prev_lags[child_player_id] = []
                if child_player_id not in prev_drifts:
                    prev_drifts[child_player_id] = []

                # calculate lag (player is too slow in relation to the master)
                lag = (
                    master_player.elapsed_milliseconds
                    - child_player.elapsed_milliseconds
                )
                prev_lags[child_player_id].append(lag)
                if len(prev_lags[child_player_id]) == 10:
                    # if we have 10 samples calclate the average lag
                    avg_lag = sum(prev_lags[child_player_id]) / len(
                        prev_lags[child_player_id]
                    )
                    prev_lags[child_player_id] = []
                    if avg_lag > 50:
                        LOGGER.debug(
                            "child player %s is lagging behind with %s milliseconds",
                            child_player_id,
                            avg_lag,
                        )
                        # we correct the lag by pausing the master player for a very short time
                        await master_player.async_cmd_pause()
                        # sending the command takes some time, account for that too
                        if avg_lag > 20:
                            sleep_time = avg_lag - 20
                            await asyncio.sleep(sleep_time / 1000)
                        asyncio.create_task(master_player.async_cmd_play())
                        break  # no more processing this round if we've just corrected a lag

                # calculate drift (player is going faster in relation to the master)
                drift = (
                    child_player.elapsed_milliseconds
                    - master_player.elapsed_milliseconds
                )
                prev_drifts[child_player_id].append(drift)
                if len(prev_drifts[child_player_id]) == 10:
                    # if we have 10 samples calculate the average drift
                    avg_drift = sum(prev_drifts[child_player_id]) / len(
                        prev_drifts[child_player_id]
                    )
                    prev_drifts[child_player_id] = []

                    if avg_drift > 50:
                        LOGGER.debug(
                            "child player %s is drifting ahead with %s milliseconds",
                            child_player_id,
                            avg_drift,
                        )
                        # we correct the drift by pausing the player for a very short time
                        # this is not the best approach but works with all playertypes
                        # temporary solution until I find something better like sending more/less pcm chunks
                        await child_player.async_cmd_pause()
                        # sending the command takes some time, account for that too
                        if avg_drift > 20:
                            sleep_time = drift - 20
                            await asyncio.sleep(sleep_time / 1000)
                        await child_player.async_cmd_play()
                        break  # no more processing this round if we've just corrected a lag
