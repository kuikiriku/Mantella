import logging
from typing import Any, Hashable
import regex
from src.games.equipment import Equipment, EquipmentItem
from src.games.external_character_info import external_character_info
from src.games.gameable import gameable
from src.conversation.action import action
from src.llm.sentence import sentence
from src.output_manager import ChatManager
from src.remember.remembering import remembering
from src.remember.summaries import summaries
from src.config.config_loader import ConfigLoader
from src.llm.openai_client import openai_client
from src.conversation.conversation import conversation
from src.conversation.context import context
from src.character_manager import Character
import src.utils as utils
from src.http.communication_constants import communication_constants as comm_consts

class CharacterDoesNotExist(Exception):
    """Exception raised when NPC name cannot be found in skyrim_characters.csv/fallout4_characters.csv"""
    pass


class GameStateManager:
    TOKEN_LIMIT_PERCENT: float = 0.45 # not used?
    WORLD_ID_CLEANSE_REGEX: regex.Pattern = regex.compile('[^A-Za-z0-9]+')

    def __init__(self, game: gameable, chat_manager: ChatManager, config: ConfigLoader, language_info: dict[Hashable, str], client: openai_client):        
        self.__game: gameable = game
        self.__config: ConfigLoader = config
        self.__language_info: dict[Hashable, str] = language_info 
        self.__client: openai_client = client
        self.__chat_manager: ChatManager = chat_manager
        self.__rememberer: remembering = summaries(game, config.memory_prompt, config.resummarize_prompt, client, language_info['language'])
        self.__talk: conversation | None = None
        self.__actions: list[action] =  [action(comm_consts.ACTION_NPC_OFFENDED, config.offended_npc_response, f"The player offended the NPC"),
                                                action(comm_consts.ACTION_NPC_FORGIVEN, config.forgiven_npc_response, f"The player made up with the NPC"),
                                                action(comm_consts.ACTION_NPC_FOLLOW, config.follow_npc_response, f"The NPC is willing to follow the player")]

    ###### react to calls from the game #######
    def start_conversation(self, input_json: dict[str, Any]) -> dict[str, Any]:
        if self.__talk: #This should only happen if game and server are out of sync due to some previous error -> close conversation and start a new one
            self.__talk.end()
            self.__talk = None
        world_id = "default"
        if input_json.__contains__(comm_consts.KEY_STARTCONVERSATION_WORLDID):
            world_id = input_json[comm_consts.KEY_STARTCONVERSATION_WORLDID]
            world_id = self.WORLD_ID_CLEANSE_REGEX.sub("", world_id)
        context_for_conversation = context(world_id, self.__config, self.__client, self.__rememberer, self.__language_info, self.__client.is_text_too_long)
        self.__talk = conversation(context_for_conversation, self.__chat_manager, self.__rememberer, self.__client, self.__actions)
        self.__update_context(input_json)
        self.__talk.start_conversation()
        
        return {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED}
    
    def continue_conversation(self, input_json: dict[str, Any]) -> dict[str, Any]:
        if(not self.__talk ):
            return self.error_message("No running conversation at this point")
        
        if input_json.__contains__(comm_consts.KEY_REQUEST_EXTRA_ACTIONS):
            extra_actions: list[str] = input_json[comm_consts.KEY_REQUEST_EXTRA_ACTIONS]
            if extra_actions.__contains__(comm_consts.ACTION_RELOADCONVERSATION):
                self.__talk.reload_conversation()

        replyType, sentence_to_play = self.__talk.continue_conversation()
        reply: dict[str, Any] = {comm_consts.KEY_REPLYTYPE: replyType}
        if sentence_to_play:
            if not sentence_to_play.error_message:
                self.__game.prepare_sentence_for_game(sentence_to_play, self.__talk.context, self.__config)            
                reply[comm_consts.KEY_REPLYTYPE_NPCTALK] = self.sentence_to_json(sentence_to_play)
            else:
                self.__talk.end()
                return self.error_message(sentence_to_play.error_message)
        return reply

    def player_input(self, input_json: dict[str, Any]) -> dict[str, Any]:
        if(not self.__talk ):
            return self.error_message("No running conversation at this point")
        
        player_text: str = input_json[comm_consts.KEY_REQUESTTYPE_PLAYERINPUT]
        self.__update_context(input_json)
        self.__talk.process_player_input(player_text)
        return {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_NPCTALK}

    def end_conversation(self, input_json: dict[str, Any]) -> dict[str, Any]:
        if(self.__talk):
            self.__talk.end()
            self.__talk = None

        logging.log(24, '\nConversations not starting when you select an NPC? See here:\nhttps://art-from-the-machine.github.io/Mantella/pages/issues_qna')
        logging.log(24, '\nWaiting for player to select an NPC...')
        return {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_ENDCONVERSATION}

    ####### JSON constructions #########

    def character_to_json(self, character_to_jsonfy: Character) -> dict[str, Any]:
        return {
            comm_consts.KEY_ACTOR_BASEID: character_to_jsonfy.base_id,
            comm_consts.KEY_ACTOR_NAME: character_to_jsonfy.name,
        }
    
    def sentence_to_json(self, sentence_to_prepare: sentence) -> dict[str, Any]:
        return {
            comm_consts.KEY_ACTOR_SPEAKER: sentence_to_prepare.speaker.name,
            comm_consts.KEY_ACTOR_LINETOSPEAK: sentence_to_prepare.sentence.strip(),
            comm_consts.KEY_ACTOR_VOICEFILE: sentence_to_prepare.voice_file,
            comm_consts.KEY_ACTOR_DURATION: sentence_to_prepare.voice_line_duration,
            comm_consts.KEY_ACTOR_ACTIONS: sentence_to_prepare.actions
        }

    ##### utils #######

    def __update_context(self,  json: dict[str, Any]):
        if self.__talk:
            actors_in_json: list[Character] = []
            for actorJson in json[comm_consts.KEY_ACTORS]:
                actor: Character | None = self.load_character(actorJson)                
                if actor:
                    actors_in_json.append(actor)
            
            self.__talk.add_or_update_character(actors_in_json)
            location: str = json[comm_consts.KEY_CONTEXT].get(comm_consts.KEY_CONTEXT_LOCATION, None)
            time: int = json[comm_consts.KEY_CONTEXT][comm_consts.KEY_CONTEXT_TIME]
            ingame_events: list[str] = json[comm_consts.KEY_CONTEXT][comm_consts.KEY_CONTEXT_INGAMEEVENTS]
            custom_context_values: dict[str, Any] = {}
            weather = ""
            if json[comm_consts.KEY_CONTEXT].__contains__(comm_consts.KEY_CONTEXT_WEATHER):
                weather = self.__game.get_weather_description(json[comm_consts.KEY_CONTEXT][comm_consts.KEY_CONTEXT_WEATHER])
            if json[comm_consts.KEY_CONTEXT].__contains__(comm_consts.KEY_CONTEXT_CUSTOMVALUES):
                custom_context_values = json[comm_consts.KEY_CONTEXT][comm_consts.KEY_CONTEXT_CUSTOMVALUES]
            self.__talk.update_context(location, time, ingame_events, weather, custom_context_values)


    # def debugging_setup(self, debug_character_name, character_df):
    #     """Select character based on debugging parameters"""

    #     # None == in-game character chosen by spell
    #     if debug_character_name == 'None':
    #         character_id, character_name = self.load_character_name_id()
    #     else:
    #         character_name = debug_character_name
    #         debug_character_name = ''

    #     character_name, character_id, location, in_game_time = self.write_dummy_game_info(character_name, character_df)

    #     return character_name, character_id, location, in_game_time
    
    @utils.time_it
    def load_character(self, json: dict[str, Any]) -> Character | None:
        try:
            base_id: str = utils.convert_to_skyrim_hex_format(str(json[comm_consts.KEY_ACTOR_BASEID]))
            ref_id: str = utils.convert_to_skyrim_hex_format(str(json[comm_consts.KEY_ACTOR_REFID]))
            character_name: str = str(json[comm_consts.KEY_ACTOR_NAME])
            gender: int = int(json[comm_consts.KEY_ACTOR_GENDER])
            race: str = str(json[comm_consts.KEY_ACTOR_RACE])
            actor_voice_model: str = str(json[comm_consts.KEY_ACTOR_VOICETYPE])
            ingame_voice_model: str = actor_voice_model.split('<')[1].split(' ')[0]
            is_in_combat: bool = bool(json[comm_consts.KEY_ACTOR_ISINCOMBAT])
            is_enemy: bool = bool(json[comm_consts.KEY_ACTOR_ISENEMY])
            relationship_rank: int = int(json[comm_consts.KEY_ACTOR_RELATIONSHIPRANK])
            custom_values: dict[str, Any] = {}
            if json.__contains__(comm_consts.KEY_ACTOR_CUSTOMVALUES):
                custom_values = json[comm_consts.KEY_ACTOR_CUSTOMVALUES]
                if not custom_values:
                    custom_values: dict[str, Any] = {}
            equipment = Equipment({})
            if json.__contains__(comm_consts.KEY_ACTOR_EQUIPMENT):
                equipment = Equipment(self.__convert_to_equipment_item_dictionary(json[comm_consts.KEY_ACTOR_EQUIPMENT]))
            is_generic_npc: bool = False
            bio: str = ""
            tts_voice_model: str = ""
            csv_in_game_voice_model: str = ""
            advanced_voice_model: str = ""
            voice_accent: str = ""
            is_player_character: bool = bool(json[comm_consts.KEY_ACTOR_ISPLAYER])
            if self.__talk and self.__talk.contains_character(ref_id):
                already_loaded_character: Character | None = self.__talk.get_character(ref_id)
                if already_loaded_character:
                    bio = already_loaded_character.bio
                    tts_voice_model = already_loaded_character.tts_voice_model
                    csv_in_game_voice_model = already_loaded_character.csv_in_game_voice_model
                    advanced_voice_model = already_loaded_character.advanced_voice_model
                    voice_accent = already_loaded_character.voice_accent
                    is_generic_npc = already_loaded_character.is_generic_npc
            elif self.__talk and not is_player_character :#If this is not the player and the character has not already been loaded
                external_info: external_character_info = self.__game.load_external_character_info(base_id, character_name, race, gender, actor_voice_model)
                
                bio = external_info.bio
                tts_voice_model = external_info.tts_voice_model
                csv_in_game_voice_model = external_info.csv_in_game_voice_model
                advanced_voice_model = external_info.advanced_voice_model
                voice_accent = external_info.voice_accent
                is_generic_npc = external_info.is_generic_npc
                if is_generic_npc:
                    character_name = external_info.name
                    ingame_voice_model = external_info.ingame_voice_model
            elif self.__talk and is_player_character and self.__config.voice_player_input:
                if custom_values.__contains__(comm_consts.KEY_ACTOR_PC_VOICEMODEL):
                    tts_voice_model = self.__get_player_voice_model(str(custom_values[comm_consts.KEY_ACTOR_PC_VOICEMODEL]))
                else:
                    tts_voice_model = self.__get_player_voice_model(None)

            return Character(base_id,
                            ref_id,
                            character_name,
                            gender,
                            race,
                            is_player_character,
                            bio,
                            is_in_combat,
                            is_enemy,
                            relationship_rank,
                            is_generic_npc,
                            ingame_voice_model,
                            tts_voice_model,
                            csv_in_game_voice_model,
                            advanced_voice_model,
                            voice_accent,
                            equipment,
                            custom_values)
        except CharacterDoesNotExist:                 
            logging.log(23, 'Restarting...')
            return None 
        
    def error_message(self, message: str) -> dict[str, Any]:
        return {
                comm_consts.KEY_REPLYTYPE: "error",
                "mantella_message": message
            }
    
    def __get_player_voice_model(self, game_value: str | None) -> str:
        if game_value == None:
            return self.__config.player_voice_model
        return game_value
    
    def __convert_to_equipment_item_dictionary(self, input_dict: dict[str, Any]) -> dict[str, EquipmentItem]:
        result: dict[str, EquipmentItem] = {}
        if input_dict:
            for slot, itemname in input_dict.items():
                result[slot] = EquipmentItem(itemname)
        return result

