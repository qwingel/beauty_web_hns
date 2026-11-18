from django import forms

from .steam import steam_id_variants


class BanLookupForm(forms.Form):
    steam_id = forms.CharField(
        label="SteamID",
        max_length=64,
        widget=forms.TextInput(attrs={"placeholder": "STEAM_0:1:123456", "autocapitalize": "off"}),
    )

    def clean_steam_id(self):
        steam_id = self.cleaned_data["steam_id"]
        if not steam_id_variants(steam_id):
            raise forms.ValidationError("Введите SteamID, SteamID3 или SteamID64.")
        return steam_id


class UnbanTicketForm(forms.Form):
    ban_id = forms.TypedChoiceField(label="Бан", coerce=int)
    message = forms.CharField(
        label="Почему нужно разбанить?",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
