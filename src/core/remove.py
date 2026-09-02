import json
import os
import shutil
from src.config.log_helper import setup_logging

logger = setup_logging(__file__)

"""
I will recieve the following json from the frontend to delete the card or sub_section:
{
    "section_id": "d08f8a68-7543-4076-ba87-e00bd9d8c6345",
    "subsection_id": None
}
or 
{
    "id": "41e6aea9-1a70-4dce-bea2-cd3527c00388",
    "subsection": "subsection_id(232323p-2242-22232)"
}

"""

def remove_card(cards_for_db, fe_json_for_delete):

    if "id" in fe_json_for_delete and fe_json_for_delete["subsection"] is None:
        id = fe_json_for_delete["id"]
        for idx, card in enumerate(cards_for_db):
            if card['section'][0]['id'] == id:
                cards_for_db.pop(idx)
                return cards_for_db

    elif "subsection" in fe_json_for_delete and fe_json_for_delete["subsection"] is not None:
        id = fe_json_for_delete["subsection"]
        for idx, card in enumerate(cards_for_db):
            for sub_idx, sub_section in enumerate(card["sub_sections"]):
                if sub_section["id"] == id:
                    cards_for_db[idx]["sub_sections"].pop(sub_idx)
                    return cards_for_db

def extract_toc(cards_for_db):
    try:
        table_of_contents = ""
        for idx,section in enumerate(cards_for_db[4:]):
            section_name = section['section'][0]['name'].lstrip('#').strip()
            modified_section_name = section_name.replace(" ", "-").strip()
            table_of_contents += f"[{idx+1}. {section_name}](#{idx+1}.-{modified_section_name})\n"
            for sub_idx,sub_section in enumerate(section['sub_sections']):
                sub_section_name = sub_section['name'].lstrip('#').strip()
                sub_section_name = sub_section_name.lstrip('- ').strip()
                modified_sub_section_name = sub_section_name.lstrip('- ').strip()
                modified_sub_section_name = modified_sub_section_name.replace(" ", "-")
                table_of_contents += f"[{idx+1}.{sub_idx+1}. {sub_section_name}](#{idx+1}.{sub_idx+1}.-{modified_sub_section_name})\n"
        return table_of_contents
    except Exception as e:
        logger.error(f"Error extracting and toc: {e}")
        return ""

def extract_toc_after_delete(cards_for_db, fe_json_for_delete):
    cards_for_db = remove_card(cards_for_db, fe_json_for_delete)
    updated_toc = extract_toc(cards_for_db)
    cards_for_db[2]["section"][0]["content"] = updated_toc
    return updated_toc