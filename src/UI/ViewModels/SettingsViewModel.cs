using System.Collections.ObjectModel;
using System.IO;
using System.Text.Json;
using System.Windows.Input;

namespace HedgeFund.UI.ViewModels;

/// <summary>
/// ViewModel вкладки Настройки.
/// Хранит токены брокеров, сохраняет в JSON файл рядом с exe.
/// </summary>
public class SettingsViewModel : BaseViewModel
{
    private static readonly string SettingsPath = Path.Combine(
        AppDomain.CurrentDomain.BaseDirectory, "settings.json");

    private string _finamToken = string.Empty;
    private string _alfaToken = string.Empty;
    private string _statusMessage = string.Empty;

    public SettingsViewModel()
    {
        SaveCommand = new RelayCommand(Save);
        LoadFromEnvCommand = new RelayCommand(LoadFromEnv);

        // Загружаем при создании
        Load();
    }

    // === Свойства ===

    public string FinamToken
    {
        get => _finamToken;
        set => SetField(ref _finamToken, value);
    }

    public string AlfaToken
    {
        get => _alfaToken;
        set => SetField(ref _alfaToken, value);
    }

    public string StatusMessage
    {
        get => _statusMessage;
        set => SetField(ref _statusMessage, value);
    }

    /// <summary>Маскированный токен Финам для отображения</summary>
    public string FinamTokenMasked => MaskToken(_finamToken);

    /// <summary>Маскированный токен Альфа для отображения</summary>
    public string AlfaTokenMasked => MaskToken(_alfaToken);

    /// <summary>Есть ли сохранённый токен Финам</summary>
    public bool HasFinamToken => !string.IsNullOrWhiteSpace(_finamToken);

    /// <summary>Есть ли сохранённый токен Альфа</summary>
    public bool HasAlfaToken => !string.IsNullOrWhiteSpace(_alfaToken);

    // === Команды ===

    public ICommand SaveCommand { get; }
    public ICommand LoadFromEnvCommand { get; }

    // === Методы ===

    /// <summary>Сохранить токены в settings.json</summary>
    private void Save()
    {
        try
        {
            var data = new SettingsData
            {
                FinamToken = _finamToken,
                AlfaToken = _alfaToken
            };

            var json = JsonSerializer.Serialize(data, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(SettingsPath, json);

            StatusMessage = $"✅ Сохранено ({DateTime.Now:HH:mm:ss})";
            OnPropertyChanged(nameof(FinamTokenMasked));
            OnPropertyChanged(nameof(AlfaTokenMasked));
            OnPropertyChanged(nameof(HasFinamToken));
            OnPropertyChanged(nameof(HasAlfaToken));
        }
        catch (Exception ex)
        {
            StatusMessage = $"❌ Ошибка сохранения: {ex.Message}";
        }
    }

    /// <summary>Загрузить токены из settings.json</summary>
    public void Load()
    {
        try
        {
            if (!File.Exists(SettingsPath)) return;

            var json = File.ReadAllText(SettingsPath);
            var data = JsonSerializer.Deserialize<SettingsData>(json);
            if (data == null) return;

            _finamToken = data.FinamToken ?? string.Empty;
            _alfaToken = data.AlfaToken ?? string.Empty;

            OnPropertyChanged(nameof(FinamToken));
            OnPropertyChanged(nameof(AlfaToken));
            OnPropertyChanged(nameof(FinamTokenMasked));
            OnPropertyChanged(nameof(AlfaTokenMasked));
            OnPropertyChanged(nameof(HasFinamToken));
            OnPropertyChanged(nameof(HasAlfaToken));
        }
        catch { }
    }

    /// <summary>Загрузить токены из переменных окружения</summary>
    private void LoadFromEnv()
    {
        var finam = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        var alfa = Environment.GetEnvironmentVariable("ALFA_TOKEN");

        if (!string.IsNullOrEmpty(finam))
        {
            FinamToken = finam;
            StatusMessage = "📋 Финам токен загружен из ENV";
        }

        if (!string.IsNullOrEmpty(alfa))
        {
            AlfaToken = alfa;
            StatusMessage += (StatusMessage.Length > 0 ? "; " : "") + "📋 Альфа токен загружен из ENV";
        }

        if (string.IsNullOrEmpty(finam) && string.IsNullOrEmpty(alfa))
        {
            StatusMessage = "⚠ Переменные FINAM_TOKEN / ALFA_TOKEN не найдены";
        }
    }

    private static string MaskToken(string token)
    {
        if (string.IsNullOrEmpty(token)) return "—";
        if (token.Length <= 8) return "****";
        return token[..4] + "..." + token[^4..] + $" ({token.Length} симв.)";
    }

    // === Модель данных ===

    private class SettingsData
    {
        public string? FinamToken { get; set; }
        public string? AlfaToken { get; set; }
    }
}
