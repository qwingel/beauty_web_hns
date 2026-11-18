from django import forms


class UnbanTicketForm(forms.Form):
    ban_id = forms.TypedChoiceField(label="Бан", coerce=int)
    message = forms.CharField(
        label="Почему нужно разбанить?",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
