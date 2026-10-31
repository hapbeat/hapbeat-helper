// HapbeatSceneLog: writes a Hapbeat Scene recording log (hapbeat-contracts specs/scene-recording.md section 5)
// into the Unreal log, one record per line after the marker "HAPBEAT_SCENE ". `hapbeat-helper scene session
// --log <project>/Saved/Logs/<Project>.log` reads the lines after the last start record.
//
// Copy this header into your game module and call it from your recording code: Begin -> (Take, the action,
// TakeEnd) per take -> End. Call Cue / Level where the game fires a haptic. Frame is the recording frame
// (frame i is game time i / fps), the same number as the f<frame>.png you capture.
//
// Record in an Editor build with -NoSound -HapbeatNoNetwork (nothing is played or sent; -HapbeatNoNetwork does
// not work in packaged games).
#pragma once

#include "CoreMinimal.h"

namespace HapbeatSceneLog
{
	inline FString Quote(const FString& S)
	{
		FString Out = TEXT("\"");
		for (const TCHAR C : S)
		{
			switch (C)
			{
			case TEXT('"'): Out += TEXT("\\\""); break;
			case TEXT('\\'): Out += TEXT("\\\\"); break;
			case TEXT('\n'): Out += TEXT("\\n"); break;
			case TEXT('\r'): Out += TEXT("\\r"); break;
			case TEXT('\t'): Out += TEXT("\\t"); break;
			default:
				if (C < 0x20) { Out += FString::Printf(TEXT("\\u%04x"), static_cast<int32>(C)); }
				else { Out.AppendChar(C); }
			}
		}
		return Out + TEXT("\"");
	}

	inline FString Num(double V) { return FMath::IsFinite(V) ? FString::SanitizeFloat(V) : TEXT("0"); }

	inline FString List(const TArray<FString>& Values)
	{
		TArray<FString> Quoted;
		for (const FString& V : Values) { Quoted.Add(Quote(V)); }
		return TEXT("[") + FString::Join(Quoted, TEXT(",")) + TEXT("]");
	}

	inline void Write(const FString& Json) { UE_LOG(LogTemp, Display, TEXT("HAPBEAT_SCENE %s"), *Json); }

	inline void Begin(const FString& Project, double Fps)
	{
		Write(FString::Printf(TEXT("{\"t\":\"start\",\"schema_version\":1,\"project\":%s,\"fps\":%s}"), *Quote(Project), *Num(Fps)));
	}

	/** A take of the script starts. Expect: cues that must fire in it; Allow: cues that may. */
	inline void Take(int32 Frame, const FString& Label, const TArray<FString>& Expect = {}, const TArray<FString>& Allow = {})
	{
		Write(FString::Printf(TEXT("{\"t\":\"take\",\"frame\":%d,\"label\":%s,\"expect\":%s,\"allow\":%s}"),
			Frame, *Quote(Label), *List(Expect), *List(Allow)));
	}

	inline void TakeEnd(int32 Frame) { Write(FString::Printf(TEXT("{\"t\":\"take_end\",\"frame\":%d}"), Frame)); }

	/** Hand: "left" / "right" / "both" or empty. Gain < 0 = not written (1). Dist (cm) < 0 = not written. */
	inline FString Extras(const FString& Hand, double Gain, double Dist, const FString& Note)
	{
		FString Out;
		if (!Hand.IsEmpty()) { Out += TEXT(",\"hand\":") + Quote(Hand); }
		if (Gain >= 0) { Out += TEXT(",\"gain\":") + Num(Gain); }
		if (Dist >= 0) { Out += TEXT(",\"dist\":") + Num(Dist); }
		if (!Note.IsEmpty()) { Out += TEXT(",\"note\":") + Quote(Note); }
		return Out;
	}

	/** A cue fired: "cue" or "cue:variant". */
	inline void Cue(int32 Frame, const FString& CueName, const FString& Hand = TEXT(""), double Gain = -1, double Dist = -1, const FString& Note = TEXT(""))
	{
		Write(FString::Printf(TEXT("{\"t\":\"cue\",\"frame\":%d,\"cue\":%s%s}"), Frame, *Quote(CueName), *Extras(Hand, Gain, Dist, Note)));
	}

	/** A fire named by the SDK entry ("unreal:<id>"); hapbeat-scene.json bindings map it to a cue. */
	inline void Source(int32 Frame, const FString& SourceId, const FString& Hand = TEXT(""), double Gain = -1, double Dist = -1, const FString& Note = TEXT(""))
	{
		Write(FString::Printf(TEXT("{\"t\":\"cue\",\"frame\":%d,\"source\":%s%s}"), Frame, *Quote(SourceId), *Extras(Hand, Gain, Dist, Note)));
	}

	/** A loop cue's input from this frame on, e.g. (Frame, "rub:level:left", 0.4). Write it when it changes. */
	inline void Level(int32 Frame, const FString& Column, double Value)
	{
		Write(FString::Printf(TEXT("{\"t\":\"level\",\"frame\":%d,\"values\":{%s:%s}}"), Frame, *Quote(Column), *Num(Value)));
	}

	/** A loop's input named by its SDK entry source; bindings give the cue (and the hand when empty). */
	inline void LevelSource(int32 Frame, const FString& SourceId, double Value, const FString& Hand = TEXT(""))
	{
		const FString HandField = Hand.IsEmpty() ? FString() : TEXT(",\"hand\":") + Quote(Hand);
		Write(FString::Printf(TEXT("{\"t\":\"level\",\"frame\":%d,\"source\":%s,\"value\":%s%s}"), Frame, *Quote(SourceId), *Num(Value), *HandField));
	}

	/** The script failed; the recording is rejected. */
	inline void Fail(const FString& Reason, int32 Frame = -1)
	{
		const FString At = Frame >= 0 ? FString::Printf(TEXT(",\"frame\":%d"), Frame) : FString();
		Write(FString::Printf(TEXT("{\"t\":\"fail\",\"reason\":%s%s}"), *Quote(Reason), *At));
	}

	/** Frames frames (0 .. Frames - 1) were captured. */
	inline void End(int32 Frames) { Write(FString::Printf(TEXT("{\"t\":\"end\",\"frames\":%d}"), Frames)); }
}
