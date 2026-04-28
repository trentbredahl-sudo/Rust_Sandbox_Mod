using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using Oxide.Core;
using Oxide.Core.Plugins;
using Oxide.Game.Rust.Cui;
using Oxide.Core.Libraries.Covalence;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Facepunch.Models.Leaderboard;

namespace Oxide.Plugins
{
    [Info("Sandbox", "Trent", "1.1.0")]
    [Description("A modular creative mode sandbox tool with a dynamic spawn menu.")]
    partial class Sandbox : CovalencePlugin
    {
        #region Variables
        private const string UI_Main = "Sandbox.Main";
        private const string UI_Header = "Sandbox.Header";
        private const string UI_Content = "Sandbox.Content";
        private const string UI_Footer = "Sandbox.Footer";

        private readonly string PanelColor = "0.1 0.1 0.1 0.8";

        private int GridCols = 10;
        private int GridRows = 6;
        private int ItemsPerPage => GridCols * GridRows;

        private readonly HashSet<ulong> OpenUIs = new HashSet<ulong>();
        private readonly Dictionary<ulong, int> PlayerSelections = new Dictionary<ulong, int>();
        private readonly Dictionary<ulong, int> PlayerPages = new Dictionary<ulong, int>();

        private readonly List<SpawnableEntry> AssetIndex = new List<SpawnableEntry>();
        private readonly List<SpawnableEntry> PrefabLibrary = new List<SpawnableEntry>();
        private Dictionary<string, string> RemoteIconMapping = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        #endregion

        #region Initialization
        void OnServerInitialized()
        {
            FetchRemoteIcons();
            Puts("Sandbox Plugin Loaded! Type /sb or Middle-Click to open the menu.");
        }

        void Unload()
        {
            foreach (var player in BasePlayer.activePlayerList)
            {
                CuiHelper.DestroyUi(player, UI_Main);
            }
        }

        private void FetchRemoteIcons()
        {
            // URL provided by user
            string url = "https://raw.githubusercontent.com/trentbredahl-sudo/Rust_Sandbox_Mod/refs/heads/master/Rust/server/carbon/plugins/Entity_list.json";

            Puts("Fetching remote icon mapping from GitHub...");
            webrequest.Enqueue(url, null, (code, response) =>
            {
                if (code != 200 || string.IsNullOrEmpty(response))
                {
                    Puts($"Warning: Failed to fetch remote icon mapping from GitHub (Code: {code}). Check your .json!");
                    LoadLibrary();
                    return;
                }

                try
                {
                    var rawData = JsonConvert.DeserializeObject<Dictionary<string, object>>(response);
                    RemoteIconMapping = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

                    foreach (var kvp in rawData)
                    {
                        if (kvp.Value is JObject nested)
                        {
                            foreach (var prop in nested.Properties())
                            {
                                RemoteIconMapping[prop.Name] = prop.Value.ToString();
                            }
                        }
                        else if (kvp.Value != null)
                        {
                            RemoteIconMapping[kvp.Key] = kvp.Value.ToString();
                        }
                    }

                    Puts($"Loaded {RemoteIconMapping.Count} icons from GitHub.");
                }
                catch (Exception ex)
                { 
                   
                    Puts($"Error parsing JSON data! {ex.Message}");           
                }

                LoadLibrary();
            }, this);
        }

        private void LoadAssetIndex()
        {
            AssetIndex.Clear();

            AssetIndex.AddRange(Interface.Oxide.DataFileSystem.ReadObject<List<SpawnableEntry>>("Sandbox/AssetIndex"));
        }

        private void LoadLibrary()
        {

            PrefabLibrary.Clear();

            foreach ( var entry in AssetIndex )
            {
                if ( entry.IsItem )
                {
                    ItemDefinition itemDef = ItemManager.FindItemDefinition(entry.ShortName);

                    if ( itemDef != null)
                    {
                        entry.ItemID = itemDef.itemid;
                    }
                }

                PrefabLibrary.Add(entry);
            }

            Puts($"Library Loaded! Found {PrefabLibrary.Count} total spawnable objects.");
        }
        #endregion

        #region UI Logic
        private void CreateSandboxUI(BasePlayer player, bool fullRedraw = false)
        {
            if (fullRedraw) CuiHelper.DestroyUi(player, UI_Main);
            CuiHelper.DestroyUi(player, UI_Header);
            CuiHelper.DestroyUi(player, UI_Content);
            CuiHelper.DestroyUi(player, UI_Footer);

            var container = new CuiElementContainer();

            if (fullRedraw)
            {
                container.Add(new CuiPanel
                {
                    Image = { Color = PanelColor },
                    RectTransform = { AnchorMin = "0.1 0.1", AnchorMax = "0.9 0.9" },
                    CursorEnabled = true
                }, "Overlay", UI_Main);
            }

            container.Add(new CuiPanel { Image = { Color = "0.2 0.2 0.2 0.5" }, RectTransform = { AnchorMin = "0 0.92", AnchorMax = "1 1" } }, UI_Main, UI_Header);
            container.Add(new CuiPanel { Image = { Color = "0 0 0 0" }, RectTransform = { AnchorMin = "0 0.08", AnchorMax = "1 0.92" } }, UI_Main, UI_Content);
            container.Add(new CuiPanel { Image = { Color = "0 0 0 0" }, RectTransform = { AnchorMin = "0 0", AnchorMax = "1 0.08" } }, UI_Main, UI_Footer);

            int currentCat = 0;
            PlayerSelections.TryGetValue(player.userID, out currentCat);
            var items = GetItemsForCategory(currentCat);

            DrawCategories(player, container, 14);
            DrawContentGrid(player, container, items);
            DrawPagination(player, container, items.Count);

            CuiHelper.AddUi(player, container);
        }

        private void DrawCategories(BasePlayer player, CuiElementContainer container, int count)
        {
            float padding = 0.01f;
            float spacing = 0.005f;
            float buttonWidth = (1.0f - (padding * 2) - (spacing * (count - 1))) / count;

            int currentSelection = 0;
            PlayerSelections.TryGetValue(player.userID, out currentSelection);

            for (int i = 0; i < count; i++)
            {
                float xMin = padding + (i * (buttonWidth + spacing));
                float xMax = xMin + buttonWidth;
                string color = (i == currentSelection) ? "0.8 0.8 0.8 1.0" : "0.3 0.3 0.3 0.8";

                container.Add(new CuiButton
                {
                    Button = { Command = $"sb.select {i}", Color = color },
                    RectTransform = { AnchorMin = $"{xMin} 0.1", AnchorMax = $"{xMax} 0.9" },
                    Text = { Text = GetCategoryName(i), Align = TextAnchor.MiddleCenter, FontSize = 10 }
                }, UI_Header, $"Category.{i}");
            }
        }

        private void DrawContentGrid(BasePlayer player, CuiElementContainer container, List<SpawnableEntry> items)
        {
            int page = 0;
            PlayerPages.TryGetValue(player.userID, out page);

            int start = page * ItemsPerPage;
            int end = Math.Min(start + ItemsPerPage, items.Count);

            float cellWidth = 1.0f / GridCols;
            float cellHeight = 1.0f / GridRows;

            for (int i = 0; i < ItemsPerPage; i++)
            {
                int itemIndex = start + i;
                if (itemIndex >= end) break;

                int row = i / GridCols;
                int col = i % GridCols;

                float xMin = col * cellWidth + 0.002f;
                float xMax = (col + 1) * cellWidth - 0.002f;
                float yMax = 1.0f - (row * cellHeight) - 0.002f;
                float yMin = 1.0f - ((row + 1) * cellHeight) + 0.002f;

                SpawnableEntry entry = items[itemIndex];

                container.Add(new CuiButton
                {
                    Button = { Command = $"sb.spawn {entry.PrefabPath}", Color = "0.2 0.2 0.2 0.8" },
                    RectTransform = { AnchorMin = $"{xMin} {yMin}", AnchorMax = $"{xMax} {yMax}" },
                    Text = { Text = "" }
                }, UI_Content, $"Item.{i}");

                if (!string.IsNullOrEmpty(entry.IconUrl))
                {
                    float iconPaddingX = 0.2f;
                    float iconPaddingY = 0.1f;

                    container.Add(new CuiElement
                    {
                        Parent = $"Item.{i}",
                        Components = {
                            new CuiRawImageComponent { Url = entry.IconUrl, Color = "1 1 1 1" },
                            new CuiRectTransformComponent {
                                AnchorMin = "0.15 0.1",
                                AnchorMax = "0.85 0.9"
                            }
                        }
                    });
                }

                container.Add(new CuiLabel
                {
                    Text = { Text = entry.ShortName, Align = TextAnchor.LowerCenter, FontSize = 8, Color = "1 1 1 0.8" },
                    RectTransform = { AnchorMin = "0 0", AnchorMax = "1 1" }
                }, $"Item.{i}");
            }
        }

        private void DrawPagination(BasePlayer player, CuiElementContainer container, int totalItems)
        {
            int page = 0;
            PlayerPages.TryGetValue(player.userID, out page);

            bool hasPrev = page > 0;
            bool hasNext = (page + 1) * ItemsPerPage < totalItems;

            container.Add(new CuiButton { Button = { Command = hasPrev ? $"sb.page {page - 1}" : "", Color = hasPrev ? "0.4 0.4 0.4 0.8" : "0.2 0.2 0.2 0.3" }, RectTransform = { AnchorMin = "0.4 0.1", AnchorMax = "0.45 0.9" }, Text = { Text = "<", Align = TextAnchor.MiddleCenter } }, UI_Footer);
            container.Add(new CuiLabel { Text = { Text = $"Page {page + 1}", Align = TextAnchor.MiddleCenter }, RectTransform = { AnchorMin = "0.45 0.1", AnchorMax = "0.55 0.9" } }, UI_Footer);
            container.Add(new CuiButton { Button = { Command = hasNext ? $"sb.page {page + 1}" : "", Color = hasNext ? "0.4 0.4 0.4 0.8" : "0.2 0.2 0.2 0.3" }, RectTransform = { AnchorMin = "0.55 0.1", AnchorMax = "0.6 0.9" }, Text = { Text = ">", Align = TextAnchor.MiddleCenter } }, UI_Footer);
        }

        private string GetCategoryFromPath(string lowerPath)
        {
            if (lowerPath.Contains("/npc/")) return "NPC";
            if (lowerPath.Contains("/vehicles/")) return "Vehicle";
            if (lowerPath.Contains("/deployable/")) return "Deployable";
            if (lowerPath.Contains("/building/")) return "Building";
            if (lowerPath.Contains("/monuments/") || lowerPath.Contains("/radtown/")) return "Monuments";
            return "World";
        }

        private string GetItemCategory(ItemDefinition item)
        {
            switch (item.category)
            {
                case ItemCategory.Weapon: return "Weapons";
                case ItemCategory.Tool: return "Tools";
                case ItemCategory.Resources: return "Resource";
                case ItemCategory.Food: return "Food";
                case ItemCategory.Ammunition: return "Ammo";
                case ItemCategory.Component: return "Components";
                case ItemCategory.Attire: return "Clothing";
                default: return "Items";
            }
        }

        private string GetCategoryName(int index)
        {
            string[] categories = { "NPC", "Vehicle", "Deployable", "Building", "Monuments", "World", "Weapons", "Tools", "Resource", "Food", "Ammo", "Components", "Clothing", "Items" };
            return (index >= 0 && index < categories.Length) ? categories[index] : "Misc";
        }

        private List<SpawnableEntry> GetItemsForCategory(int categoryIndex)
        {
            string targetCategory = GetCategoryName(categoryIndex);
            return PrefabLibrary.Where(x => x.Category == targetCategory).ToList();
        }
        #endregion

        #region Commands & Input

        [ChatCommand("sb")]
        private void SandboxCommand(BasePlayer player, string command, string[] args) { ToggleSandboxUI(player); }

        [ConsoleCommand("sb.select")]
        private void CmdSelectCategory(ConsoleSystem.Arg arg) { var player = arg.Player(); if (player == null) return; PlayerSelections[player.userID] = arg.GetInt(0); PlayerPages[player.userID] = 0; CreateSandboxUI(player, false); }

        [ConsoleCommand("sb.page")]
        private void CmdChangePage(ConsoleSystem.Arg arg) { var player = arg.Player(); if (player == null) return; PlayerPages[player.userID] = arg.GetInt(0); CreateSandboxUI(player, false); }

        [ConsoleCommand("sb.spawn")]
        private void CmdSpawnItem(ConsoleSystem.Arg arg)
        {
            var player = arg.Player();
            if (player == null || !arg.HasArgs()) return;

            string pathOrName = arg.GetString(0);
            var entry = PrefabLibrary.FirstOrDefault(x => x.PrefabPath == pathOrName);
            if (entry == null) return;

            if (entry.IsItem)
            {
                Item item = ItemManager.CreateByItemID(entry.ItemID, 1);
                if (item != null) player.GiveItem(item);
            }
            else
            {
                RaycastHit hit;
                if (Physics.Raycast(player.eyes.HeadRay(), out hit, 100f, Physics.DefaultRaycastLayers))
                {
                    BaseEntity entity = GameManager.server.CreateEntity(entry.PrefabPath, hit.point, Quaternion.identity);
                    if (entity != null) entity.Spawn();
                }
            }
        }

        private void OnPlayerInput(BasePlayer player, InputState input) { if (input.WasJustPressed(BUTTON.FIRE_THIRD)) ToggleSandboxUI(player); }

        private void ToggleSandboxUI(BasePlayer player)
        {
            if (OpenUIs.Contains(player.userID)) { CuiHelper.DestroyUi(player, UI_Main); OpenUIs.Remove(player.userID); }
            else { CreateSandboxUI(player, true); OpenUIs.Add(player.userID); }
        }
        #endregion
    }

    public class SpawnableEntry
    {
        public string ShortName, PrefabPath, Category, IconUrl;
        public int ItemID;
        public bool IsItem;
    }
}
