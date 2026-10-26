from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm

User = get_user_model()


class LoginForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Invalid username or password.",
        "inactive": "Invalid username or password.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"autofocus": True, "autocomplete": "username", "autocapitalize": "none"}
        )
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})


class StaffCreateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "role", "phone"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["username"].help_text = "Letters, digits and . _ - only. Cannot be changed later."

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username


class StaffEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "role", "phone"]

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        if actor is not None and self.instance.pk == actor.pk:
            self.fields["role"].disabled = True
            self.fields["role"].help_text = "You cannot change your own role."


class StaffPasswordChangeForm(PasswordChangeForm):
    def clean(self):
        cleaned = super().clean()
        new = cleaned.get("new_password1")
        if new and not self.errors and self.user.check_password(new):
            self.add_error("new_password1", "The new password must be different from the current one.")
        return cleaned
